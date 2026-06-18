from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.v1.files as files_mod


def _build_client(monkeypatch, image_dir: Path, video_dir: Path) -> TestClient:
    monkeypatch.setattr(files_mod, "IMAGE_DIR", image_dir, raising=False)
    monkeypatch.setattr(files_mod, "VIDEO_DIR", video_dir, raising=False)

    app = FastAPI()
    app.include_router(files_mod.router, prefix="/v1/files")
    return TestClient(app)


def test_files_endpoint_serves_cached_file(monkeypatch, tmp_path):
    image_dir = tmp_path / "image"
    video_dir = tmp_path / "video"
    image_dir.mkdir(parents=True, exist_ok=True)
    video_dir.mkdir(parents=True, exist_ok=True)

    (image_dir / "foo-bar.jpg").write_bytes(b"ok")
    client = _build_client(monkeypatch, image_dir=image_dir, video_dir=video_dir)

    resp = client.get("/v1/files/image/foo/bar.jpg")
    assert resp.status_code == 200


def test_files_endpoint_rejects_backslash_paths(monkeypatch, tmp_path):
    image_dir = tmp_path / "image"
    video_dir = tmp_path / "video"
    image_dir.mkdir(parents=True, exist_ok=True)
    video_dir.mkdir(parents=True, exist_ok=True)

    # On POSIX, backslashes are valid filename characters. But on Windows they are
    # path separators, and treating them as part of the filename enables traversal.
    # We enforce a consistent rule: reject/normalize backslashes from user input.
    dangerous_name = "..\\..\\secret.jpg"
    (image_dir / dangerous_name).write_bytes(b"pwned")

    client = _build_client(monkeypatch, image_dir=image_dir, video_dir=video_dir)
    resp = client.get("/v1/files/image/..%5C..%5Csecret.jpg")
    assert resp.status_code == 404
