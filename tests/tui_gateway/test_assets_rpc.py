"""Tests for the assets.list JSON-RPC method on the tui_gateway server.

Assets = generated artifacts, attachments, media, and outputs. The gateway walks
media roots (and, for a scoped call, a project's own folders) and returns a flat
list the mobile app can render as a gallery with preview/download.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
import tui_gateway.server as server


def _call(method, params=None):
    handler = server._methods[method]
    resp = handler(1, params or {})
    assert "error" not in resp, resp.get("error")
    return resp["result"]


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(parents=True)
    token = set_hermes_home_override(home)
    try:
        yield home
    finally:
        reset_hermes_home_override(token)


def _write(path: Path, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_assets_method_is_registered():
    assert "assets.list" in server._methods


def test_assets_method_is_a_long_handler():
    # The disk walk must run off the dispatch thread.
    assert "assets.list" in server._LONG_HANDLERS


def test_global_assets_lists_media_roots_only(tmp_path):
    home = tmp_path / "home"
    png = _write(home / "media" / "shot.png")
    mp4 = _write(home / "cache" / "videos" / "clip.mp4")
    # A non-asset file in a media root must not be listed.
    _write(home / "media" / "notes.log")
    # A file outside every media root must not be listed.
    _write(home / "somewhere" / "stray.png")

    assets = _call("assets.list")["assets"]

    paths = {a["path"] for a in assets}
    assert str(png) in paths
    assert str(mp4) in paths
    assert str(home / "media" / "notes.log") not in paths
    assert str(home / "somewhere" / "stray.png") not in paths


def test_assets_carry_kind_size_mime_and_modified(tmp_path):
    home = tmp_path / "home"
    png = _write(home / "images" / "a.png", b"\x89PNG\r\n\x1a\n" + b"0" * 16)

    asset = _call("assets.list")["assets"][0]

    assert asset["name"] == "a.png"
    assert asset["path"] == str(png)
    assert asset["kind"] == "image"
    assert asset["size"] == 24
    assert asset["mime_type"] == "image/png"
    assert isinstance(asset["modified_at"], (int, float))


def test_assets_are_sorted_newest_first(tmp_path):
    home = tmp_path / "home"
    older = _write(home / "media" / "old.png")
    newer = _write(home / "media" / "new.png")
    os.utime(older, (1000, 1000))
    os.utime(newer, (2000, 2000))

    assets = _call("assets.list")["assets"]
    assert [a["path"] for a in assets] == [str(newer), str(older)]


def test_project_assets_scope_to_project_folders(tmp_path):
    home = tmp_path / "home"
    proj = tmp_path / "repo"
    proj.mkdir(parents=True)
    shot = _write(proj / "screenshots" / "ui.png")
    # Outside the project: must not appear in the scoped listing.
    stray = _write(home / "media" / "global.png")

    created = _call("projects.create", {"name": "Demo", "folders": [str(proj)]})["project"]

    assets = _call("assets.list", {"project_id": created["id"]})["assets"]
    paths = {a["path"] for a in assets}
    assert str(shot) in paths
    assert str(stray) not in paths


def test_global_assets_carry_null_project_id_when_unmatched(tmp_path):
    home = tmp_path / "home"
    _write(home / "media" / "global.png")

    assets = _call("assets.list")["assets"]
    assert assets[0]["project_id"] is None


def test_unknown_project_returns_error():
    resp = server._methods["assets.list"](1, {"project_id": "missing"})
    assert "error" in resp


def test_assets_never_list_sensitive_files(tmp_path):
    home = tmp_path / "home"
    _write(home / "media" / ".env")
    _write(home / "media" / "credentials")
    ok = _write(home / "media" / "fine.png")

    assets = _call("assets.list")["assets"]
    paths = {a["path"] for a in assets}
    assert str(ok) in paths
    assert str(home / "media" / ".env") not in paths
    assert str(home / "media" / "credentials") not in paths


def test_limit_caps_result(tmp_path):
    home = tmp_path / "home"
    for i in range(5):
        _write(home / "media" / f"{i}.png")

    assets = _call("assets.list", {"limit": 2})["assets"]
    assert len(assets) == 2
