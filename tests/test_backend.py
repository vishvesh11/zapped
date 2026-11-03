"""Backend API tests (in-process, FastAPI TestClient)."""
import asyncio
import datetime as dt
import threading
import time

import pytest


# ---- auth / basics -------------------------------------------------------- #
def test_health_and_pages(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert "BUZZ WIRE CHALLENGE" in client.get("/").text
    assert "Control Panel" in client.get("/admin").text


@pytest.mark.parametrize("path", ["/api/v1/config", "/api/v1/session", "/api/v1/media",
                                  "/api/v1/leaderboard"])
def test_protected_routes_require_api_key(client, path):
    assert client.get(path, headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get(path, headers={"X-API-Key": ""}).status_code == 401
    assert client.get(path).status_code == 200


def test_public_routes_need_no_key(client):
    for path in ("/public/leaderboard", "/public/session", "/public/photos"):
        assert client.get(path, headers={"X-API-Key": ""}).status_code == 200


def test_no_shock_fields_anywhere(client):
    cfg = client.get("/api/v1/config").json()
    assert not any("shock" in k for k in cfg)
    sess = client.post("/api/v1/session/start", json={"player_name": "A"}).json()
    assert not any("shock" in k for k in sess)
    spec = client.get("/openapi.json").text.lower()
    assert "shock" not in spec


# ---- config / media ------------------------------------------------------- #
def test_config_and_media(client):
    cfg = client.get("/api/v1/config").json()
    assert cfg["current_media_file"] == "chime.wav"
    assert cfg["score_base"] == 1000
    assert "chime.wav" in client.get("/api/v1/media").json()["files"]
    r = client.get("/public/media/chime.wav")
    assert r.status_code == 200 and r.content[:4] == b"RIFF"


@pytest.mark.parametrize("name", ["../config.py", "missing.wav", "chime.txt", "..%2Fmain.py"])
def test_media_rejects_bad_names(client, name):
    assert client.post("/api/v1/config", json={"current_media_file": name}).status_code == 422
    assert client.get(f"/public/media/{name}").status_code == 404


def test_config_update_requires_a_field(client):
    assert client.post("/api/v1/config", json={}).status_code == 422
    # Legacy shock-only payloads (old admin panel) are rejected instead of silently "working".
    assert client.post("/api/v1/config", json={"shock_enabled": False}).status_code == 422


# ---- session lifecycle ---------------------------------------------------- #
def test_full_game_flow(client):
    s = client.post("/api/v1/session/start", json={"player_name": "  Alice  "}).json()
    assert s["active"] and s["phase"] == "waiting" and s["player_name"] == "Alice"
    assert client.post("/api/v1/session/start", json={"player_name": "Bob"}).status_code == 409

    sid = s["session_id"]
    assert client.post("/api/v1/game/run-start", json={"session_id": "nope"}).status_code == 409
    r = client.post("/api/v1/game/run-start", json={"session_id": sid}).json()
    assert r["phase"] == "running"

    for i in range(1, 3):
        t = client.post("/api/v1/event/touch", json={"session_id": sid, "fault_count": i}).json()
        assert t == {"chime_queued": True, "running": True, "touch_count": i}
    # Device's count catches up if a report was skipped.
    t = client.post("/api/v1/event/touch", json={"session_id": sid, "fault_count": 5}).json()
    assert t["touch_count"] == 5

    entry = client.post("/api/v1/game/complete", json={
        "session_id": sid, "touch_count": 5, "completion_time_seconds": 0.5}).json()
    assert entry["player_name"] == "Alice" and entry["touch_count"] == 5 and entry["rank"] == 1
    from helpers import compute_score
    assert entry["score"] == compute_score(entry["completion_time_seconds"], 5)
    assert client.get("/api/v1/session").json()["active"] is False
    board = client.get("/public/leaderboard").json()
    assert board[0]["session_id"] == sid


def test_touch_for_stale_session_is_not_counted(client):
    sid = client.post("/api/v1/session/start", json={"player_name": "A"}).json()["session_id"]
    client.post("/api/v1/game/run-start", json={"session_id": sid})
    t = client.post("/api/v1/event/touch", json={"session_id": "old", "fault_count": 3}).json()
    assert t["running"] is False and t["touch_count"] == 0
    assert client.get("/api/v1/session").json()["touch_count"] == 0


def test_touch_before_run_start_is_ignored(client):
    client.post("/api/v1/session/start", json={"player_name": "A"})
    assert client.post("/api/v1/event/touch").json()["running"] is False


def test_complete_uses_server_time_when_device_disagrees(client):
    sid = client.post("/api/v1/session/start", json={"player_name": "A"}).json()["session_id"]
    client.post("/api/v1/game/run-start", json={"session_id": sid})
    e = client.post("/api/v1/game/complete", json={
        "session_id": sid, "touch_count": 0, "completion_time_seconds": 500}).json()
    assert e["completion_time_seconds"] < 5


def test_abort_and_validation(client):
    assert client.post("/api/v1/session/start", json={"player_name": "   "}).status_code == 422
    assert client.post("/api/v1/session/start", json={"player_name": "x" * 25}).status_code == 422
    client.post("/api/v1/session/start", json={"player_name": "A"})
    assert client.delete("/api/v1/session").json()["active"] is False
    assert client.post("/api/v1/game/complete", json={
        "session_id": "x", "touch_count": 0, "completion_time_seconds": 1}).status_code == 409


def test_leaderboard_ordering_and_clear(client):
    for name, faults in (("Slow", 3), ("Best", 0), ("Mid", 1)):
        sid = client.post("/api/v1/session/start", json={"player_name": name}).json()["session_id"]
        client.post("/api/v1/game/complete", json={
            "session_id": sid, "touch_count": faults, "completion_time_seconds": 10})
    names = [r["player_name"] for r in client.get("/api/v1/leaderboard").json()]
    assert names == ["Best", "Mid", "Slow"]
    assert client.delete("/api/v1/leaderboard").json() == {"cleared": True}
    assert client.get("/public/leaderboard").json() == []


def test_compute_score_floor():
    from helpers import compute_score
    assert compute_score(0, 0) == 1000
    assert compute_score(10, 2) == 850
    assert compute_score(1000, 100) == 0


# ---- photos / S3 ----------------------------------------------------------- #
class FakePaginator:
    def __init__(self, pages):
        self.pages = pages

    def paginate(self, **kw):
        prefix = kw["Prefix"]
        for page in self.pages:
            yield {"Contents": [o for o in page if o["Key"].startswith(prefix)]}


class FakeS3:
    def __init__(self, pages):
        self.pages = pages

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return FakePaginator(self.pages)

    def generate_presigned_url(self, op, Params, ExpiresIn):
        return "https://s3.example/" + Params["Key"]


def test_photos_paginate_and_sort_newest_first(client, monkeypatch):
    import routes
    t0 = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    page1 = [{"Key": f"sessions/a/touch_{i}.jpg", "LastModified": t0 + dt.timedelta(seconds=i)}
             for i in range(1000)]
    page2 = [{"Key": "sessions/z/touch_new.jpg", "LastModified": t0 + dt.timedelta(days=1)}]
    monkeypatch.setattr(routes, "s3_client", FakeS3([page1, page2]))
    items = client.get("/public/photos?limit=3").json()
    assert [i["key"] for i in items][0] == "sessions/z/touch_new.jpg"
    assert len(items) == 3 and items[0]["session_id"] == "z"
    only_a = client.get("/public/photos?session_id=a&limit=500").json()
    assert len(only_a) == 100  # limit is clamped
    assert all(i["session_id"] == "a" for i in only_a)


def test_photos_reject_bad_session_id(client):
    assert client.get("/public/photos?session_id=../../etc").status_code == 422


def test_camera_selftest_without_s3(client):
    r = client.post("/api/v1/camera/test?use_camera=false").json()
    assert r["uploaded"] is False and r["source"] == "dummy"
    assert "S3 client not configured" in r["error"]


def test_selftest_key_outside_public_gallery(monkeypatch):
    import helpers

    class Recorder:
        key = None

        def put_object(self, **kw):
            Recorder.key = kw["Key"]

        def generate_presigned_url(self, *a, **kw):
            return "u"

    monkeypatch.setattr(helpers, "s3_client", Recorder())
    r = helpers.camera_selftest(use_camera=False)
    assert r["uploaded"] and not Recorder.key.startswith("sessions/")


def test_touch_frame_renders_jpeg():
    import io

    from PIL import Image

    from helpers import apply_touch_frame
    buf = io.BytesIO()
    Image.new("RGB", (320, 240)).save(buf, format="JPEG")
    out = apply_touch_frame(buf.getvalue(), "Alice", 2)
    assert Image.open(io.BytesIO(out)).size == (320, 240)


# ---- SSE broadcaster ------------------------------------------------------- #
def test_broadcast_from_worker_thread_wakes_subscriber():
    """Regression: background-task broadcasts (photo_uploaded) must reach SSE clients promptly."""
    from helpers import EventBroadcaster

    async def scenario():
        b = EventBroadcaster()
        q = b.subscribe()
        loop = asyncio.get_running_loop()
        start = loop.time()
        def worker():  # like the photo-upload background task: broadcast while the loop is idle
            time.sleep(0.2)
            b.broadcast("photo_uploaded", {"k": 1})

        threading.Thread(target=worker).start()
        msg = await asyncio.wait_for(q.get(), timeout=5.0)
        latency = loop.time() - start
        b.unsubscribe(q)
        b.broadcast("x", {})  # no subscribers: no error
        return msg, latency

    msg, latency = asyncio.run(scenario())
    assert "event: photo_uploaded" in msg
    # A non-threadsafe put only gets noticed when the loop next wakes for some other reason.
    assert latency < 1.0, f"event took {latency:.2f}s to reach the subscriber"


def test_sse_stream_delivers_events(backend):
    """SSE over a real socket: connect, trigger a game event, receive it."""
    import httpx

    got = []
    ready = threading.Event()

    def listen():
        with httpx.stream("GET", backend.url + "/public/events/stream", timeout=5) as r:
            assert r.headers["cache-control"] == "no-cache"
            for line in r.iter_lines():
                if "connected" in line:
                    ready.set()
                if line.startswith("event: session_update"):
                    got.append(line)
                    return

    t = threading.Thread(target=listen, daemon=True)
    t.start()
    assert ready.wait(5)
    backend.http.post("/api/v1/session/start", json={"player_name": "SSE"}).raise_for_status()
    t.join(5)
    assert got, "session_update event not received"

