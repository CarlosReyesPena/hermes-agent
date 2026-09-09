"""Assets JSON-RPC handler: a server-authoritative index of generated artifacts,
attachments, media, and outputs.

Assets are files the agent actually produces or attaches — not arbitrary disk
contents. The global listing walks the media roots under ``HERMES_HOME``; a
project-scoped listing additionally walks the project's own folders (bounded and
pruned of VCS/build trees). Sensitive credential basenames are always excluded.

Bodies are rebound onto server.py's globals (method_ctx.bind_module) and
reference them bare.
"""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path
from typing import Optional

from .method_ctx import HandlerRegistry, bind_module

_registry = HandlerRegistry()
method = _registry.method
_profile_scoped = _registry.profile_scoped

_ASSET_EXT_KIND = {
    # images
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image",
    ".webp": "image", ".svg": "image", ".bmp": "image", ".ico": "image",
    # video
    ".mp4": "video", ".mov": "video", ".webm": "video", ".mkv": "video",
    ".avi": "video", ".m4v": "video",
    # audio
    ".mp3": "audio", ".wav": "audio", ".flac": "audio", ".m4a": "audio",
    ".ogg": "audio", ".opus": "audio",
    # documents / outputs
    ".pdf": "document", ".doc": "document", ".docx": "document",
    ".xls": "document", ".xlsx": "document", ".ppt": "document",
    ".pptx": "document", ".md": "document", ".txt": "document",
    ".csv": "document", ".json": "document", ".zip": "document",
    ".apk": "document",
}

_MIME_BY_EXT = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
    ".bmp": "image/bmp", ".ico": "image/x-icon",
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
    ".mkv": "video/x-matroska", ".avi": "video/x-msvideo", ".m4v": "video/x-m4v",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".flac": "audio/flac",
    ".m4a": "audio/mp4", ".ogg": "audio/ogg", ".opus": "audio/ogg",
    ".pdf": "application/pdf", ".md": "text/markdown", ".txt": "text/plain",
    ".csv": "text/csv", ".json": "application/json",
    ".zip": "application/zip", ".apk": "application/vnd.android.package-archive",
}

# Subtrees never walked: VCS, dependency, and build output directories.
_SKIP_DIRS = {
    ".git", ".hg", ".svn", ".cache", ".next", ".turbo", ".venv", "venv",
    "__pycache__", "build", "dist", "node_modules", "target", ".gradle",
}

# Cap the walk so a huge project folder can't stall the RPC pool.
_MAX_ASSETS = 500
_MAX_DEPTH = 6


def _media_roots() -> list[Path]:
    """Directories under HERMES_HOME where generated media and attachments live."""
    home = get_hermes_home()
    names = ("images", "screenshots", "media", "media_cache", "webui/attachments")
    roots: list[Path] = []
    for name in names:
        root = home / name
        if root.is_dir():
            roots.append(root)
    # cache holds image/audio/video subdirs produced by tools.
    cache = home / "cache"
    if cache.is_dir():
        for sub in ("images", "videos", "audio", "screenshots"):
            subdir = cache / sub
            if subdir.is_dir():
                roots.append(subdir)
    return roots


def _is_sensitive_name(name: str) -> bool:
    lowered = name.lower()
    if lowered == ".env" or lowered.startswith(".env.") or lowered == ".envrc":
        return True
    return lowered in {
        "auth.json", "auth.lock", "credentials", "config.yaml",
        ".anthropic_oauth.json", "google_token.json",
        "google_oauth_pending.json", "google_oauth.json", ".git-credentials",
    }


def _asset_entry(path: Path, project_id: Optional[str]) -> dict:
    suffix = path.suffix.lower()
    kind = _ASSET_EXT_KIND.get(suffix, "other")
    mime = _MIME_BY_EXT.get(suffix) or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    try:
        st = path.stat()
        size = st.st_size
        modified_at = st.st_mtime
    except OSError:
        size = 0
        modified_at = 0.0
    return {
        "name": path.name,
        "path": str(path),
        "kind": kind,
        "mime_type": mime,
        "size": size,
        "modified_at": modified_at,
        "project_id": project_id,
    }


def _walk_assets(roots: list[Path], project_id: Optional[str], limit: int) -> list[dict]:
    """Walk ``roots`` for asset files, newest first, capped at ``limit``."""
    found: list[dict] = []
    seen: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            depth = len(Path(dirpath).relative_to(root).parts)
            if depth > _MAX_DEPTH:
                dirnames[:] = []
                continue
            dirnames[:] = [
                d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")
            ]
            for filename in filenames:
                if _is_sensitive_name(filename):
                    continue
                path = Path(dirpath) / filename
                if path.suffix.lower() not in _ASSET_EXT_KIND:
                    continue
                real = str(path.resolve())
                if real in seen:
                    continue
                seen.add(real)
                found.append(_asset_entry(path, project_id))
                if len(found) >= limit * 4:  # over-collect then trim by recency
                    break
            if len(found) >= limit * 4:
                break
        if len(found) >= limit * 4:
            break
    found.sort(key=lambda a: a["modified_at"], reverse=True)
    return found[:limit]


def _project_folders(project_id: str) -> list[Path]:
    """The project's declared folders (or empty when unknown/missing)."""
    from hermes_cli import projects_db as pdb
    with pdb.connect_closing() as conn:
        project = pdb.get_project(conn, str(project_id))
        if project is None:
            raise _NoSuchProject
        return [Path(f.path) for f in project.folders if f.path]


class _NoSuchProject(Exception):
    pass


@method("assets.list")
@_profile_scoped
def _assets_list(rid, params: dict) -> dict:
    """``{assets: [...], project_id: <str|null>}``.

    Global (no ``project_id``): walk media roots. Scoped: walk the project's
    folders only. Each asset carries name/path/kind/mime/size/modified.
    """
    try:
        limit = int(params.get("limit") or 100)
    except (TypeError, ValueError):
        limit = 100
    limit = max(1, min(limit, 500))

    project_id = str(params.get("project_id") or "").strip() or None
    try:
        if project_id is not None:
            roots = _project_folders(project_id)
        else:
            roots = _media_roots()
    except _NoSuchProject:
        return _err(rid, 5062, "no such project")

    assets = _walk_assets(roots, project_id, limit)
    return _ok(rid, {"assets": assets, "project_id": project_id})


def register(server) -> None:
    _registry.install(server)
    server._LONG_HANDLERS = server._LONG_HANDLERS | frozenset({"assets.list"})
    bind_module(globals(), server, skip=("_",))
