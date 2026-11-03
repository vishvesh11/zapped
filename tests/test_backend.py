"""Backend API tests (in-process, FastAPI TestClient)."""
import asyncio
import datetime as dt
import threading
import time

import pytest


# ---- auth / basics -------------------------------------------------------- #
def test_health_and_pages(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert "Buzz Wire Challenge" in client.get("/").text
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



# ---- sound upload + penalty trigger --------------------------------------- #
def _wav(seconds=0.05):
    import io
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\0\0" * int(8000 * seconds))
    return buf.getvalue()


MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" * 64
OGG = b"OggS\x00\x02" + b"\0" * 64


def _capture_chimes(monkeypatch):
    import routes
    chimes = []
    original = routes.broadcaster.broadcast

    def spy(event, data):
        if event == "play_chime":
            chimes.append(data)
        original(event, data)

    monkeypatch.setattr(routes.broadcaster, "broadcast", spy)
    return chimes


@pytest.mark.parametrize("name,body", [("boo.wav", _wav()), ("boo.mp3", MP3), ("boo.ogg", OGG)])
def test_upload_media_and_serve_it(client, name, body):
    r = client.put(f"/api/v1/media/{name}", content=body)
    assert r.status_code == 200, r.text
    assert r.json()["filename"] == name and r.json()["size"] == len(body)
    assert name in client.get("/api/v1/media").json()["files"]
    served = client.get(f"/public/media/{name}")
    assert served.status_code == 200 and served.content == body


def test_upload_use_as_sets_config(client):
    r = client.put("/api/v1/media/fail.wav?use_as=penalty", content=_wav()).json()
    assert r["config"]["penalty_media_file"] == "fail.wav"
    assert r["config"]["current_media_file"] == "chime.wav"
    r = client.put("/api/v1/media/ding.wav?use_as=chime", content=_wav()).json()
    assert r["config"]["current_media_file"] == "ding.wav"
    assert client.get("/api/v1/config").json()["penalty_media_file"] == "fail.wav"


@pytest.mark.parametrize("name,body,status", [
    ("evil.txt", b"RIFF", 422),                    # wrong extension
    (".hidden.wav", _wav(), 422),                  # bad name
    ("fake.wav", b"not really audio at all", 422),  # content doesn't match type
    ("mp3-as.wav", MP3, 422),
    ("empty.wav", b"", 422),
    ("..%2F..%2Fmain.wav", _wav(), 404),           # traversal never reaches the handler
])
def test_upload_rejects_bad_files(client, name, body, status):
    assert client.put(f"/api/v1/media/{name}", content=body).status_code == status
    assert client.get("/api/v1/media").json()["files"] == ["chime.wav"]


def test_upload_rejects_oversized_file(client, monkeypatch):
    import helpers
    monkeypatch.setattr(helpers.settings, "max_media_bytes", 1000)
    assert client.put("/api/v1/media/big.wav", content=_wav(1)).status_code == 413
    assert "big.wav" not in client.get("/api/v1/media").json()["files"]


def test_upload_requires_api_key(client):
    r = client.put("/api/v1/media/x.wav", content=_wav(), headers={"X-API-Key": "wrong"})
    assert r.status_code == 401


def test_penalty_sound_plays_after_more_than_threshold_touches(client, monkeypatch):
    chimes = _capture_chimes(monkeypatch)
    client.put("/api/v1/media/fail sound.wav?use_as=penalty", content=_wav()).raise_for_status()
    sid = client.post("/api/v1/session/start", json={"player_name": "P"}).json()["session_id"]
    client.post("/api/v1/game/run-start", json={"session_id": sid}).raise_for_status()
    for i in range(1, 7):
        client.post("/api/v1/event/touch", json={"session_id": sid, "fault_count": i}).raise_for_status()

    assert [c["touch_count"] for c in chimes] == [1, 2, 3, 4, 5, 6]
    assert [c["penalty"] for c in chimes] == [False, False, False, True, True, True]
    assert chimes[0]["audio_url"] == "/public/media/chime.wav"
    assert chimes[3]["audio_url"] == "/public/media/fail%20sound.wav"
    # The URL the UI will fetch actually serves the uploaded file.
    assert client.get(chimes[3]["audio_url"]).content == _wav()


def test_penalty_threshold_is_configurable_and_handles_count_jumps(client, monkeypatch):
    chimes = _capture_chimes(monkeypatch)
    client.put("/api/v1/media/fail.wav?use_as=penalty", content=_wav()).raise_for_status()
    assert client.post("/api/v1/config", json={"penalty_threshold": 1}).json()["penalty_threshold"] == 1
    sid = client.post("/api/v1/session/start", json={"player_name": "P"}).json()["session_id"]
    client.post("/api/v1/game/run-start", json={"session_id": sid}).raise_for_status()
    client.post("/api/v1/event/touch", json={"session_id": sid, "fault_count": 1})
    client.post("/api/v1/event/touch", json={"session_id": sid, "fault_count": 4})  # coalesced report
    assert [(c["touch_count"], c["penalty"]) for c in chimes] == [(1, False), (4, True)]


def test_no_penalty_sound_means_always_chime(client, monkeypatch):
    chimes = _capture_chimes(monkeypatch)
    client.put("/api/v1/media/fail.wav?use_as=penalty", content=_wav()).raise_for_status()
    assert client.post("/api/v1/config", json={"penalty_media_file": ""}).json()["penalty_media_file"] is None
    sid = client.post("/api/v1/session/start", json={"player_name": "P"}).json()["session_id"]
    client.post("/api/v1/game/run-start", json={"session_id": sid}).raise_for_status()
    for i in range(1, 6):
        client.post("/api/v1/event/touch", json={"session_id": sid, "fault_count": i})
    assert {c["audio_url"] for c in chimes} == {"/public/media/chime.wav"}
    assert not any(c["penalty"] for c in chimes)


@pytest.mark.parametrize("body", [
    {"penalty_media_file": "missing.wav"},
    {"penalty_media_file": "../config.py"},
    {"penalty_threshold": -1},
    {"penalty_threshold": 101},
])
def test_penalty_config_validation(client, body):
    assert client.post("/api/v1/config", json=body).status_code == 422


def test_sound_settings_survive_restart(client):
    import helpers
    client.put("/api/v1/media/fail.wav?use_as=penalty", content=_wav()).raise_for_status()
    client.post("/api/v1/config", json={"penalty_threshold": 5}).raise_for_status()
    helpers.state.reset_sounds()          # simulate a fresh process...
    helpers.state.load_sounds()           # ...loading what the lifespan hook loads
    cfg = client.get("/api/v1/config").json()
    assert cfg["penalty_media_file"] == "fail.wav" and cfg["penalty_threshold"] == 5

    # A saved sound that has since disappeared is dropped instead of breaking playback.
    (helpers.settings.media_dir_resolved / "fail.wav").unlink()
    helpers.state.load_sounds()
    assert client.get("/api/v1/config").json()["penalty_media_file"] is None


# ---- teams ------------------------------------------------------------------ #
def _play(client, name, team, touches, seconds):
    body = {"player_name": name}
    if team is not None:
        body["team"] = team
    sid = client.post("/api/v1/session/start", json=body).json()["session_id"]
    client.post("/api/v1/game/run-start", json={"session_id": sid}).raise_for_status()
    return client.post("/api/v1/game/complete", json={
        "session_id": sid, "touch_count": touches, "completion_time_seconds": seconds}).json()


def test_team_presets_are_public(client):
    teams = client.get("/public/teams").json()["teams"]
    for t in ("Data Science", "Cybersecurity", "HR"):
        assert t in teams


@pytest.mark.parametrize("raw,stored", [
    ("data science", "Data Science"),        # snaps to the preset spelling
    ("  CYBERSECURITY ", "Cybersecurity"),
    ("Platform   Crew", "Platform Crew"),    # custom names kept, whitespace collapsed
    ("   ", None),
])
def test_team_is_normalized(client, raw, stored):
    s = client.post("/api/v1/session/start", json={"player_name": "N", "team": raw}).json()
    assert s["team"] == stored
    assert client.get("/public/session").json()["team"] == stored


def test_team_name_length_limit(client):
    r = client.post("/api/v1/session/start", json={"player_name": "N", "team": "x" * 33})
    assert r.status_code == 422


def test_team_saved_on_leaderboard(client):
    entry = _play(client, "Ann", "HR", 1, 10)
    assert entry["team"] == "HR"
    assert client.get("/public/leaderboard").json()[0]["team"] == "HR"


def test_team_leaderboard_uses_median_of_best_scores(client):
    from helpers import compute_score
    # (times stay under 10 s: larger device/server disagreements fall back to server time)
    # Data Science: three players. Median of best scores = Bea's score.
    _play(client, "Al", "Data Science", 0, 3)     # best overall
    _play(client, "Bea", "Data Science", 2, 6)
    _play(client, "Cy", "Data Science", 6, 9)
    _play(client, "cy ", "data science", 5, 8)    # same player, other spelling: better run counts
    # HR: one superstar and one weak run -> median is the midpoint.
    _play(client, "Dee", "HR", 0, 2)
    _play(client, "Eve", "HR", 9, 9)
    # No team: ignored for team standings.
    _play(client, "Solo", None, 0, 1)

    teams = client.get("/public/leaderboard/teams").json()
    by = {t["team"]: t for t in teams}
    assert set(by) == {"Data Science", "HR"}

    ds = by["Data Science"]
    assert ds["players"] == 3 and ds["runs"] == 4
    assert ds["median_score"] == compute_score(6, 2)
    assert ds["best_player"] == "Al" and ds["best_score"] == compute_score(3, 0)
    assert ds["median_time_seconds"] == 6 and ds["median_touches"] == 2

    hr = by["HR"]
    assert hr["players"] == 2
    assert hr["median_score"] == round((compute_score(2, 0) + compute_score(9, 9)) / 2, 1)

    assert [t["rank"] for t in teams] == [1, 2]
    assert teams[0]["median_score"] >= teams[1]["median_score"]


def test_team_ties_prefer_more_players(client):
    _play(client, "A", "Sales", 1, 10)
    _play(client, "B", "Legal", 1, 10)
    _play(client, "C", "Legal", 1, 10)
    teams = client.get("/public/leaderboard/teams").json()
    assert [t["team"] for t in teams] == ["Legal", "Sales"]


def test_team_leaderboard_empty_and_cleared(client):
    assert client.get("/public/leaderboard/teams").json() == []
    _play(client, "A", "Finance", 0, 3)
    assert len(client.get("/public/leaderboard/teams").json()) == 1
    client.delete("/api/v1/leaderboard").raise_for_status()
    assert client.get("/public/leaderboard/teams").json() == []


def test_old_database_gets_team_column(tmp_path):
    import sqlite3
    from helpers import Leaderboard
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE entries (id INTEGER PRIMARY KEY AUTOINCREMENT, player_name TEXT NOT NULL,
                    touch_count INTEGER NOT NULL, completion_time_seconds REAL NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    conn.execute("INSERT INTO entries (player_name, touch_count, completion_time_seconds) VALUES ('Old', 1, 9)")
    conn.commit()
    conn.close()
    board = Leaderboard(str(db))
    try:
        assert board.top(5)[0].team is None
        board.add("s1", "New", 0, 5, 900, "HR")
        assert {e.player_name: e.team for e in board.top(5)} == {"Old": None, "New": "HR"}
        assert [t.team for t in board.teams()] == ["HR"]
    finally:
        board.close()
