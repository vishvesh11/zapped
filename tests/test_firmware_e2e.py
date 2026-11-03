"""End-to-end: real firmware (simulated pins) <-> real backend process <-> admin API."""
import asyncio
import json

import httpx
import pytest

from conftest import API_KEY
from firmware_sim import FakePin, load_firmware, wait_for

FAST = dict(SESSION_POLL_S=0.1, CONFIG_POLL_S=1, MIN_RUN_MS=300, FAULT_LOCKOUT_MS=200,
            DEBOUNCE_MS=10, HOOK_DEBOUNCE_MS=20, USE_WATCHDOG=False, HTTP_TIMEOUT_S=3)


def live_overrides(backend, **extra):
    return {**FAST, "BENCH_MODE": False, "BACKEND_HOST": backend.url, "API_KEY": API_KEY, **extra}


async def run_firmware(fw, scenario):
    m = fw.main
    tasks = [asyncio.create_task(t) for t in (m.session_task(), m.game_task(), m.config_task())]
    try:
        await scenario()
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def test_firmware_has_no_shock_code():
    for path in ("config.py", "main.py", "net.py", "boot.py", "buzzer_test.py", "wiring_check.py"):
        from conftest import FIRMWARE_DIR
        text = (FIRMWARE_DIR / path).read_text().lower()
        assert "shock" not in text and "relay" not in text, path
    assert not (FIRMWARE_DIR / "relay_test.py").exists()


@pytest.mark.parametrize("host,port,expected", [
    ("https://diwaligame.vishvesh.me", 443, ("diwaligame.vishvesh.me", 443, True)),
    ("https://diwaligame.vishvesh.me/", 8000, ("diwaligame.vishvesh.me", 8000, True)),
    ("https://example.com", 80, ("example.com", 443, True)),
    ("http://192.168.5.82:8000", 443, ("192.168.5.82", 8000, False)),
    ("http://10.0.0.2", 443, ("10.0.0.2", 80, False)),
    ("192.168.5.82", 8000, ("192.168.5.82", 8000, False)),
    ("example.com", 443, ("example.com", 443, True)),
])
def test_parse_backend(host, port, expected):
    with load_firmware() as fw:
        assert fw.net.parse_backend(host, port) == expected


def test_full_game_over_http(backend):
    """Admin starts a game; wand hangs/lifts; two track touches; END hook -> leaderboard."""
    cfg = None

    async def scenario():
        nonlocal cfg
        m, c = fw.main, fw.config
        api = httpx.AsyncClient(base_url=backend.url, headers={"X-API-Key": API_KEY})
        events = []

        async def sse():
            async with httpx.AsyncClient(timeout=None) as sc:
                async with sc.stream("GET", backend.url + "/public/events/stream") as r:
                    event = None
                    async for line in r.aiter_lines():
                        if line.startswith("event: "):
                            event = line[7:]
                        elif line.startswith("data: "):
                            events.append((event, json.loads(line[6:])))

        sse_task = asyncio.create_task(sse())
        try:
            # Touches while IDLE are ignored entirely.
            FakePin.levels[c.TRACK_PIN] = 0
            await asyncio.sleep(0.2)
            FakePin.levels[c.TRACK_PIN] = 1
            assert m.state.faults == 0

            sid = (await api.post("/api/v1/session/start", json={"player_name": "Pico"})).json()["session_id"]
            await wait_for(lambda: m.state.phase == m.READY, what="firmware READY")
            assert m.state.session_id == sid

            FakePin.levels[c.START_HOOK_PIN] = 0          # hang wand on START
            await wait_for(lambda: m.state.hook_seen, what="hook seen")
            FakePin.levels[c.START_HOOK_PIN] = 1          # lift -> run starts
            await wait_for(lambda: m.state.phase == m.RUNNING, what="RUNNING")

            async def backend_phase():
                return (await api.get("/api/v1/session")).json()["phase"] == "running"
            await wait_for(backend_phase, what="backend running")

            for n in (1, 2):
                FakePin.levels[c.TRACK_PIN] = 0
                await wait_for(lambda: m.state.faults == n, what=f"fault {n}")
                FakePin.levels[c.TRACK_PIN] = 1
                await asyncio.sleep(c.FAULT_LOCKOUT_MS / 1000 + 0.05)

            async def backend_faults():
                return (await api.get("/api/v1/session")).json()["touch_count"] == 2
            await wait_for(backend_faults, what="backend counted 2 touches")

            await asyncio.sleep(c.MIN_RUN_MS / 1000)
            FakePin.levels[c.END_HOOK_PIN] = 0            # hang on END
            await wait_for(lambda: m.state.phase == m.IDLE and m.state.pending is None,
                           what="result reported")
            FakePin.levels[c.END_HOOK_PIN] = 1

            board = (await api.get("/public/leaderboard")).json()
            assert board[0]["player_name"] == "Pico"
            assert board[0]["touch_count"] == 2
            assert board[0]["session_id"] == sid
            assert (await api.get("/api/v1/session")).json()["active"] is False

            await wait_for(lambda: any(e == "game_complete" for e, _ in events), what="SSE game_complete")
            chimes = [d for e, d in events if e == "play_chime"]
            assert len(chimes) == 2 and chimes[0]["audio_url"] == "/public/media/chime.wav"

            # After the game the wand is disarmed: touches don't count or reach the backend.
            FakePin.levels[c.TRACK_PIN] = 0
            await asyncio.sleep(0.2)
            assert m.state.faults == 2
            cfg = m.state
        finally:
            sse_task.cancel()
            await api.aclose()

    with load_firmware(**live_overrides(backend)) as fw:
        asyncio.run(run_firmware(fw, scenario))
    assert cfg is not None


def test_abort_from_panel_returns_device_to_idle(backend):
    async def scenario():
        m = fw.main
        backend.http.post("/api/v1/session/start", json={"player_name": "Abort"}).raise_for_status()
        await wait_for(lambda: m.state.phase == m.READY, what="READY")
        backend.http.delete("/api/v1/session").raise_for_status()
        await wait_for(lambda: m.state.phase == m.IDLE and m.state.session_id is None, what="IDLE")

    with load_firmware(**live_overrides(backend)) as fw:
        asyncio.run(run_firmware(fw, scenario))


def test_wrong_api_key_keeps_device_idle(backend):
    async def scenario():
        m = fw.main
        backend.http.post("/api/v1/session/start", json={"player_name": "Nope"}).raise_for_status()
        await asyncio.sleep(0.5)
        assert m.state.phase == m.IDLE
        assert m.state.backend_failures > 0
        assert FakePin.outputs.get("LED") == 0   # LED off = backend not usable

    with load_firmware(**live_overrides(backend, API_KEY="wrong")) as fw:
        asyncio.run(run_firmware(fw, scenario))


def test_score_formula_synced_from_backend(backend):
    async def scenario():
        fw.config.SCORE_BASE = 1
        assert await fw.main.fetch_config()
        assert fw.config.SCORE_BASE == 1000
        assert fw.main.preview_score(10, 2) == 850

    with load_firmware(**live_overrides(backend)) as fw:
        asyncio.run(scenario())


def test_backend_down_is_handled(backend):
    async def scenario():
        status, data = await fw.net.request("GET", "/api/v1/session")
        assert (status, data) == (None, None)

    with load_firmware(**live_overrides(backend, BACKEND_HOST="http://127.0.0.1:1",
                                        HTTP_TIMEOUT_S=1)) as fw:
        asyncio.run(scenario())


def test_net_handles_chunked_and_non_json_responses():
    """Proxies like Cloudflare may re-chunk responses or return HTML error pages."""
    seen = {}

    async def handler(reader, writer):
        req = b""
        while not req.endswith(b"\r\n\r\n"):
            req += await reader.read(1)
        seen["req"] = req.decode()
        if b"/html" in req:
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 6\r\n\r\n<html>")
        else:
            body = b'{"active": true, "session_id": "abc"}'
            writer.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
            writer.write(b"%x\r\n%s\r\n" % (10, body[:10]))
            writer.write(b"%x;ext=1\r\n%s\r\n0\r\n\r\n" % (len(body) - 10, body[10:]))
        await writer.drain()
        writer.close()

    async def scenario():
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        fw.net.HOST, fw.net.PORT, fw.net.USE_TLS = "127.0.0.1", port, False
        fw.net.HOST_HEADER = "127.0.0.1:%d" % port
        async with server:
            assert await fw.net.request("GET", "/api/v1/session") == (200, {"active": True, "session_id": "abc"})
            assert "User-Agent: buzzwire-pico" in seen["req"]
            assert "X-API-Key: k" in seen["req"]
            assert await fw.net.request("GET", "/html") == (502, None)

    with load_firmware(BENCH_MODE=False, API_KEY="k", HTTP_TIMEOUT_S=2) as fw:
        asyncio.run(scenario())


def test_bench_mode_game_without_backend():
    """BENCH_MODE: hold START 2 s to arm, lift, touch, END -> finishes locally."""
    async def scenario():
        m, c = fw.main, fw.config
        FakePin.levels[c.START_HOOK_PIN] = 0
        await wait_for(lambda: m.state.phase == m.READY, timeout=5, what="bench armed")
        FakePin.levels[c.START_HOOK_PIN] = 1
        await wait_for(lambda: m.state.phase == m.RUNNING, what="bench running")
        FakePin.levels[c.TRACK_PIN] = 0
        await wait_for(lambda: m.state.faults == 1, what="bench fault")
        FakePin.levels[c.TRACK_PIN] = 1
        await asyncio.sleep(c.MIN_RUN_MS / 1000)
        FakePin.levels[c.END_HOOK_PIN] = 0
        await wait_for(lambda: m.state.phase == m.IDLE and m.state.pending is None, what="bench finished")
        assert m.bench["active"] is False

    with load_firmware(**dict(FAST, BENCH_MODE=True)) as fw:
        asyncio.run(run_firmware(fw, scenario))
