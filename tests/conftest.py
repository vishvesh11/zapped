"""Shared fixtures.

* In-process backend (FastAPI TestClient) for fast API tests.
* A real backend process (uvicorn) for end-to-end tests: firmware simulation,
  browser UI and the Streamlit panel all talk to it over real HTTP.
"""
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = ROOT / "backend"
FIRMWARE_DIR = ROOT / "firmware"
API_KEY = "test-key"

# Configure the in-process backend *before* anything imports it.
_TMP = Path(tempfile.mkdtemp(prefix="buzzwire-tests-"))
os.environ.update({
    "GAME_API_KEY": API_KEY,
    "DB_PATH": str(_TMP / "unit.db"),
    "MEDIA_DIR": str(_TMP / "media-unit"),
    "S3_ACCESS_KEY": "",
    "S3_SECRET_KEY": "",
})
sys.path.insert(0, str(BACKEND_DIR))


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    import helpers
    from main import app

    with TestClient(app) as c:
        c.headers["X-API-Key"] = API_KEY
        helpers.state.session = None
        helpers.get_board().clear()
        yield c
        helpers.state.session = None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LiveBackend:
    def __init__(self, url: str):
        self.url = url
        self.http = httpx.Client(base_url=url, headers={"X-API-Key": API_KEY}, timeout=5)

    def reset(self):
        self.http.delete("/api/v1/session").raise_for_status()
        self.http.delete("/api/v1/leaderboard").raise_for_status()


@pytest.fixture(scope="session")
def live_backend():
    port = _free_port()
    env = dict(os.environ)
    env.update({
        "GAME_API_KEY": API_KEY,
        "DB_PATH": str(_TMP / "live.db"),
        "MEDIA_DIR": str(_TMP / "media-live"),
        "S3_ACCESS_KEY": "",
        "S3_SECRET_KEY": "",
        "PYTHONUNBUFFERED": "1",
    })
    log_path = _TMP / "uvicorn.log"
    log_file = open(log_path, "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1",
         "--port", str(port), "--workers", "1"],
        cwd=BACKEND_DIR, env=env, stdout=log_file, stderr=subprocess.STDOUT,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 20
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("backend exited early:\n" + log_path.read_text())
        try:
            if httpx.get(url + "/health", timeout=0.5).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    else:
        proc.kill()
        raise RuntimeError("backend did not start:\n" + log_path.read_text())

    backend = LiveBackend(url)
    yield backend
    backend.http.close()
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    log_file.close()


@pytest.fixture()
def backend(live_backend):
    live_backend.reset()
    yield live_backend
    live_backend.reset()
