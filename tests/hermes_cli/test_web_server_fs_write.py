"""Tests for the free-form /api/fs/* write endpoints (mkdir, upload, delete,
move, copy) used by the Android file explorer.
"""

import base64
from pathlib import Path

import pytest

from hermes_cli import web_server

pytest.importorskip("starlette.testclient")
from starlette.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    previous_auth_required = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.auth_required = False
    test_client = TestClient(web_server.app)
    test_client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    try:
        yield test_client
    finally:
        if previous_auth_required is None:
            try:
                delattr(web_server.app.state, "auth_required")
            except AttributeError:
                pass
        else:
            web_server.app.state.auth_required = previous_auth_required


def _data_url(path: Path) -> str:
    return f"data:application/octet-stream;base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def test_fs_mkdir_creates_directory(client, tmp_path):
    target = tmp_path / "new-folder"

    response = client.post("/api/fs/mkdir", json={"path": str(target)})

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert target.is_dir()


def test_fs_mkdir_conflict_on_existing_file(client, tmp_path):
    target = tmp_path / "existing"
    target.write_text("x")

    response = client.post("/api/fs/mkdir", json={"path": str(target)})

    assert response.status_code == 409


def test_fs_upload_writes_file(client, tmp_path):
    target = tmp_path / "uploaded.txt"
    source = tmp_path / "payload.bin"
    source.write_bytes(b"\x00\x01\x02")

    response = client.post(
        "/api/fs/upload",
        json={"path": str(target), "data_url": _data_url(source), "overwrite": False},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert target.read_bytes() == b"\x00\x01\x02"


def test_fs_upload_refuses_overwrite_without_flag(client, tmp_path):
    target = tmp_path / "uploaded.txt"
    target.write_text("keep-me")
    source = tmp_path / "payload.bin"
    source.write_bytes(b"new")

    response = client.post(
        "/api/fs/upload",
        json={"path": str(target), "data_url": _data_url(source), "overwrite": False},
    )

    assert response.status_code == 409
    assert target.read_text() == "keep-me"


def test_fs_delete_removes_file(client, tmp_path):
    target = tmp_path / "gone.txt"
    target.write_text("bye")

    response = client.post(
        "/api/fs/delete", json={"path": str(target), "recursive": False},
    )

    assert response.status_code == 200
    assert not target.exists()


def test_fs_delete_recursive_removes_directory(client, tmp_path):
    target = tmp_path / "dir"
    (target / "sub").mkdir(parents=True)
    (target / "sub" / "file.txt").write_text("x")

    response = client.post(
        "/api/fs/delete", json={"path": str(target), "recursive": True},
    )

    assert response.status_code == 200
    assert not target.exists()


def test_fs_delete_non_recursive_refuses_nonempty_dir(client, tmp_path):
    target = tmp_path / "dir"
    target.mkdir()
    (target / "file.txt").write_text("x")

    response = client.post(
        "/api/fs/delete", json={"path": str(target), "recursive": False},
    )

    assert response.status_code == 409
    assert target.is_dir()


def test_fs_delete_refuses_sensitive_file(client, tmp_path):
    target = tmp_path / ".env"
    target.write_text("SECRET=1")

    response = client.post(
        "/api/fs/delete", json={"path": str(target), "recursive": False},
    )

    assert response.status_code == 403
    assert target.exists()


def test_fs_move_renames_file(client, tmp_path):
    source = tmp_path / "old.txt"
    source.write_text("content")
    destination = tmp_path / "new.txt"

    response = client.post(
        "/api/fs/move",
        json={"source": str(source), "destination": str(destination)},
    )

    assert response.status_code == 200
    assert not source.exists()
    assert destination.read_text() == "content"


def test_fs_move_into_other_directory(client, tmp_path):
    src_dir = tmp_path / "src"
    dst_dir = tmp_path / "dst"
    src_dir.mkdir()
    dst_dir.mkdir()
    source = src_dir / "file.txt"
    source.write_text("x")

    response = client.post(
        "/api/fs/move",
        json={"source": str(source), "destination": str(dst_dir / "file.txt")},
    )

    assert response.status_code == 200
    assert not source.exists()
    assert (dst_dir / "file.txt").read_text() == "x"


def test_fs_copy_duplicates_file(client, tmp_path):
    source = tmp_path / "orig.txt"
    source.write_text("copy me")
    destination = tmp_path / "copy.txt"

    response = client.post(
        "/api/fs/copy",
        json={"source": str(source), "destination": str(destination)},
    )

    assert response.status_code == 200
    assert source.read_text() == "copy me"
    assert destination.read_text() == "copy me"


def test_fs_copy_directory_recursively(client, tmp_path):
    source = tmp_path / "orig-dir"
    (source / "sub").mkdir(parents=True)
    (source / "sub" / "file.txt").write_text("nested")
    destination = tmp_path / "copy-dir"

    response = client.post(
        "/api/fs/copy",
        json={"source": str(source), "destination": str(destination)},
    )

    assert response.status_code == 200
    assert (destination / "sub" / "file.txt").read_text() == "nested"
    # Original still intact.
    assert (source / "sub" / "file.txt").read_text() == "nested"


def test_fs_copy_rejects_directory_inside_itself(client, tmp_path):
    source = tmp_path / "source"
    source.mkdir()

    response = client.post(
        "/api/fs/copy",
        json={
            "source": str(source),
            "destination": str(source / "nested-copy"),
        },
    )

    assert response.status_code == 400
    assert not (source / "nested-copy").exists()


def test_fs_move_rejects_directory_inside_itself(client, tmp_path):
    source = tmp_path / "source"
    source.mkdir()

    response = client.post(
        "/api/fs/move",
        json={
            "source": str(source),
            "destination": str(source / "nested-move"),
        },
    )

    assert response.status_code == 400
    assert source.exists()


def test_fs_write_endpoints_require_auth(tmp_path):
    """Write endpoints are the highest-risk surface; they must be auth-gated."""
    anonymous = TestClient(web_server.app)
    target = tmp_path / "victim.txt"
    target.write_text("secret")

    responses = [
        anonymous.post("/api/fs/mkdir", json={"path": str(tmp_path / "d")}),
        anonymous.post(
            "/api/fs/upload",
            json={"path": str(target), "data_url": "data:application/octet-stream;base64,eA=="},
        ),
        anonymous.post("/api/fs/delete", json={"path": str(target)}),
        anonymous.post(
            "/api/fs/move",
            json={"source": str(target), "destination": str(tmp_path / "moved")},
        ),
        anonymous.post(
            "/api/fs/copy",
            json={"source": str(target), "destination": str(tmp_path / "copied")},
        ),
    ]

    assert all(response.status_code == 401 for response in responses)
    assert target.exists()  # Nothing was written or removed anonymously.
