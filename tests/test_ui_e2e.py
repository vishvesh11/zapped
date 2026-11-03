"""Browser end-to-end tests for /admin and / (headless Chromium via Playwright),
plus a smoke test of the Streamlit panel. Device actions are simulated with the
same API calls the Pico makes."""
import os

import pytest

from conftest import API_KEY

sync_api = pytest.importorskip("playwright.sync_api")

TAILWIND_STUB = """document.addEventListener('DOMContentLoaded', () => {
  const st = document.createElement('style');
  st.textContent = '.hidden{display:none}.flex{display:flex}';
  document.head.appendChild(st);
});"""


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # browser binaries not installed
            pytest.skip(f"chromium unavailable: {exc}")
        yield b
        b.close()


@pytest.fixture()
def page_factory(browser, backend):
    pages = []

    def make(path, api_key=API_KEY):
        ctx = browser.new_context()
        if api_key is not None:
            ctx.add_init_script(f"localStorage.setItem('BUZZ_API_KEY', {api_key!r});")
        page = ctx.new_page()
        page.errors = []
        page.on("pageerror", lambda e: page.errors.append(str(e)))
        # Keep the tests offline/deterministic: styling CDN isn't needed for behaviour.
        # Stub only the utility classes the pages toggle for visibility.
        page.route("https://cdn.tailwindcss.com/**", lambda route: route.fulfill(body=TAILWIND_STUB, content_type="text/javascript"))
        page.route("https://fonts.googleapis.com/**", lambda route: route.fulfill(body="", content_type="text/css"))
        page.route("https://fonts.gstatic.com/**", lambda route: route.abort())
        page.goto(backend.url + path)
        pages.append(ctx)
        return page

    yield make
    for ctx in pages:
        assert all(not p.errors for p in ctx.pages), [p.errors for p in ctx.pages]
        ctx.close()


def device_play(backend, sid, touches, seconds=4.2):
    backend.http.post("/api/v1/game/run-start", json={"session_id": sid}).raise_for_status()
    for i in range(1, touches + 1):
        backend.http.post("/api/v1/event/touch", json={"session_id": sid, "fault_count": i}).raise_for_status()
    return backend.http.post("/api/v1/game/complete", json={
        "session_id": sid, "touch_count": touches, "completion_time_seconds": seconds}).json()


def test_admin_has_no_shock_controls(page_factory):
    page = page_factory("/admin")
    html = page.content().lower()
    assert "shock" not in html and "consent" not in html
    sync_api.expect(page.locator("#backend-status")).to_have_text("Backend: connected")
    sync_api.expect(page.locator("#score-formula")).to_contain_text("1000")
    sync_api.expect(page.locator("#media-file")).to_have_value("chime.wav")


def test_admin_full_game(page_factory, backend):
    page = page_factory("/admin")
    sync_api.expect(page.locator("#backend-status")).to_have_text("Backend: connected")

    page.fill("#player-name", "Alice")
    page.click("text=Start game")
    sync_api.expect(page.locator("#flash")).to_contain_text("team")    # team is required
    page.select_option("#player-team", "Data Science")
    page.click("text=Start game")
    sync_api.expect(page.locator("#flash")).to_have_text("Game started.")
    sync_api.expect(page.locator("#st-player")).to_have_text("Alice")
    sync_api.expect(page.locator("#st-team")).to_have_text("Data Science")
    sync_api.expect(page.locator("#session-message")).to_contain_text("Waiting for the player")

    sid = backend.http.get("/api/v1/session").json()["session_id"]
    backend.http.post("/api/v1/game/run-start", json={"session_id": sid}).raise_for_status()
    sync_api.expect(page.locator("#session-message")).to_contain_text("Running")
    backend.http.post("/api/v1/event/touch", json={"session_id": sid, "fault_count": 1})
    sync_api.expect(page.locator("#st-touches")).to_have_text("1")

    backend.http.post("/api/v1/game/complete", json={
        "session_id": sid, "touch_count": 1, "completion_time_seconds": 3.0}).raise_for_status()
    sync_api.expect(page.locator("#leaderboard-body tr").first).to_contain_text("Alice")
    sync_api.expect(page.locator("#session-message")).to_contain_text("No active game")


def test_admin_settings_abort_and_reset(page_factory, backend):
    page = page_factory("/admin")
    sync_api.expect(page.locator("#media-file")).to_have_value("chime.wav")
    page.click("text=Apply settings")
    sync_api.expect(page.locator("#flash")).to_have_text("Settings applied.")

    page.fill("#player-name", "Bob")
    page.select_option("#player-team", "__other__")
    page.fill("#player-team-other", "  Platform   Crew ")
    page.click("text=Start game")
    sync_api.expect(page.locator("#st-player")).to_have_text("Bob")
    assert backend.http.get("/api/v1/session").json()["team"] == "Platform Crew"
    # The custom team is offered (and kept selected) for the next player.
    sync_api.expect(page.locator("#player-team")).to_have_value("Platform Crew")
    page.click("text=Abort game")
    sync_api.expect(page.locator("#flash")).to_have_text("Game aborted.")
    assert backend.http.get("/api/v1/session").json()["active"] is False

    sid = backend.http.post("/api/v1/session/start", json={"player_name": "Carol"}).json()["session_id"]
    device_play(backend, sid, 0)
    sync_api.expect(page.locator("#leaderboard-body")).to_contain_text("Carol")
    page.click("text=Reset leaderboard")
    page.check("#reset-confirm")
    page.click("#reset-button")
    sync_api.expect(page.locator("#leaderboard-body")).to_contain_text("No completed games yet.")


def _wav_bytes():
    import io
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\0\0" * 800)
    return buf.getvalue()


def test_admin_upload_penalty_sound_and_trigger(page_factory, backend):
    """Upload from the admin page, check the browser can decode it, then verify that
    touches 1-3 play the chime and touches 4+ switch the page's audio to the upload."""
    page = page_factory("/admin")
    sync_api.expect(page.locator("#backend-status")).to_have_text("Backend: connected")
    sync_api.expect(page.locator("#penalty-file")).to_have_value("")
    sync_api.expect(page.locator("#penalty-summary")).to_contain_text("every touch plays the chime")

    page.set_input_files("#upload-file", files=[{
        "name": "oops.wav", "mimeType": "audio/wav", "buffer": _wav_bytes()}])
    page.select_option("#upload-use-as", "penalty")
    page.click("#upload-btn")
    sync_api.expect(page.locator("#flash")).to_have_text("Uploaded oops.wav and set it as the penalty sound.")
    sync_api.expect(page.locator("#penalty-file")).to_have_value("oops.wav")
    sync_api.expect(page.locator("#penalty-summary")).to_contain_text("from touch 4 on")
    assert backend.http.get("/api/v1/config").json()["penalty_media_file"] == "oops.wav"

    # The browser can actually load and decode the uploaded file.
    decoded = page.evaluate("""() => new Promise(resolve => {
        const a = new Audio('/public/media/oops.wav');
        a.oncanplaythrough = () => resolve(a.duration);
        a.onerror = () => resolve(-1);
    })""")
    assert decoded > 0

    # Live trigger: watch which file the page's chime element is switched to per touch.
    sid = backend.http.post("/api/v1/session/start", json={"player_name": "Dee"}).json()["session_id"]
    backend.http.post("/api/v1/game/run-start", json={"session_id": sid}).raise_for_status()
    for i in range(1, 6):
        backend.http.post("/api/v1/event/touch", json={"session_id": sid, "fault_count": i}).raise_for_status()
        sync_api.expect(page.locator("#st-touches")).to_have_text(str(i))
        src = page.evaluate("document.getElementById('chime').src")
        assert src.endswith("/oops.wav" if i > 3 else "/chime.wav"), (i, src)

    # Threshold can be changed from the form.
    page.fill("#penalty-threshold", "1")
    page.click("text=Apply settings")
    sync_api.expect(page.locator("#flash")).to_have_text("Settings applied.")
    sync_api.expect(page.locator("#penalty-summary")).to_contain_text("from touch 2 on")
    assert backend.http.get("/api/v1/config").json()["penalty_threshold"] == 1


def test_admin_upload_rejects_non_audio(page_factory):
    page = page_factory("/admin")
    sync_api.expect(page.locator("#backend-status")).to_have_text("Backend: connected")
    page.set_input_files("#upload-file", files=[{
        "name": "notes.wav", "mimeType": "audio/wav", "buffer": b"definitely not audio"}])
    page.click("#upload-btn")
    sync_api.expect(page.locator("#flash")).to_contain_text("Upload failed: 422")
    page.errors.clear()  # the failed fetch is expected


def test_admin_camera_test_reports_missing_s3(page_factory):
    page = page_factory("/admin")
    page.click("text=Test S3 only (dummy frame)")
    sync_api.expect(page.locator("#camera-result")).to_contain_text("S3 client not configured")
    sync_api.expect(page.locator("#flash")).to_contain_text("Test failed")


def test_admin_wrong_api_key(page_factory):
    page = page_factory("/admin", api_key="bad")
    sync_api.expect(page.locator("#backend-status")).to_have_text("Backend: API key rejected (401)")
    page.fill("#player-name", "X")
    page.click("text=Start game")
    sync_api.expect(page.locator("#flash")).to_contain_text("not loaded")


def test_public_page_leaderboard_banner_and_xss(page_factory, backend):
    evil = "<img src=x onerror=a=1>"
    sid = backend.http.post("/api/v1/session/start", json={"player_name": evil}).json()["session_id"]
    device_play(backend, sid, 2)

    page = page_factory("/", api_key=None)
    row = page.locator("#leaderboard-body tr").first
    sync_api.expect(row).to_contain_text(evil)          # shown literally, not parsed as HTML
    assert page.locator("#leaderboard-body img").count() == 0
    assert page.evaluate("typeof window.a") == "undefined"
    assert "zap" not in page.content().lower()

    # Live banner follows the game via SSE.
    backend.http.post("/api/v1/session/start", json={"player_name": "Dave"}).raise_for_status()
    sync_api.expect(page.locator("#live-player")).to_have_text("Dave")
    sid2 = backend.http.get("/api/v1/session").json()["session_id"]
    backend.http.post("/api/v1/game/run-start", json={"session_id": sid2})
    backend.http.post("/api/v1/event/touch", json={"session_id": sid2, "fault_count": 1})
    sync_api.expect(page.locator("#live-touches")).to_have_text("1")
    sync_api.expect(page.locator("#live-phase")).to_have_text("running")
    backend.http.post("/api/v1/game/complete", json={
        "session_id": sid2, "touch_count": 1, "completion_time_seconds": 2})
    sync_api.expect(page.locator("#live-banner")).to_be_hidden()
    sync_api.expect(page.locator("#leaderboard-body")).to_contain_text("Dave")

    # Clicking a player filters the gallery (empty without S3) and names them safely.
    page.locator("#leaderboard-body tr", has_text="Dave").click()
    sync_api.expect(page.locator("#gallery-title")).to_have_text("📸 Dave's Touch Photos")
    sync_api.expect(page.locator("#photo-grid")).to_contain_text("No touch photos found for Dave")
    page.click("#reset-gallery-btn")
    sync_api.expect(page.locator("#gallery-title")).to_have_text("📸 Latest Touch Moments")


def _play_api(backend, name, team, touches, seconds):
    sid = backend.http.post("/api/v1/session/start", json={"player_name": name, "team": team}).json()["session_id"]
    return device_play(backend, sid, touches, seconds)


def test_public_page_podium_teams_tab_and_filter(page_factory, backend):
    _play_api(backend, "Ada", "Data Science", 0, 3)
    _play_api(backend, "Bo", "Data Science", 3, 6)
    _play_api(backend, "Cleo", "Cybersecurity", 1, 5)
    _play_api(backend, "Dan", "HR", 8, 9)

    page = page_factory("/", api_key=None)
    sync_api.expect(page.locator("#leaderboard-body tr")).to_have_count(4)
    sync_api.expect(page.locator("#leaderboard-body tr").first).to_contain_text("Ada")
    sync_api.expect(page.locator("#leaderboard-body tr").first).to_contain_text("Data Science")
    sync_api.expect(page.locator("#podium")).to_be_visible()
    sync_api.expect(page.locator("#podium")).to_contain_text("Ada")
    sync_api.expect(page.locator("#stat-players")).to_have_text("4")
    sync_api.expect(page.locator("#stat-teams")).to_have_text("3")

    # Filter players by team.
    page.select_option("#team-filter", "Data Science")
    sync_api.expect(page.locator("#leaderboard-body tr")).to_have_count(2)
    sync_api.expect(page.locator("#leaderboard-body")).not_to_contain_text("Cleo")
    page.select_option("#team-filter", "")
    sync_api.expect(page.locator("#leaderboard-body tr")).to_have_count(4)

    # Team tab: ranked by median, filter hidden.
    page.click("#tab-teams")
    sync_api.expect(page.locator("#panel-teams")).to_be_visible()
    sync_api.expect(page.locator("#panel-players")).to_be_hidden()
    sync_api.expect(page.locator("#team-filter-wrap")).to_be_hidden()
    rows = page.locator("#team-body .team-row")
    sync_api.expect(rows).to_have_count(3)
    expected = [t["team"] for t in backend.http.get("/public/leaderboard/teams").json()]
    assert [rows.nth(i).locator(".team-name").inner_text() for i in range(3)] == expected
    sync_api.expect(rows.filter(has_text="Data Science")).to_contain_text("2 players")

    # A finished game shows a toast, refreshes the team tab, and the live card shows the team.
    sid = backend.http.post("/api/v1/session/start", json={"player_name": "Eli", "team": "HR"}).json()["session_id"]
    sync_api.expect(page.locator("#live-team")).to_have_text("HR")
    device_play(backend, sid, 0, 1)
    sync_api.expect(page.locator("#toast-wrap")).to_contain_text("Eli finished #1")
    sync_api.expect(rows.filter(has_text="HR")).to_contain_text("2 players")

    page.click("#tab-players")
    sync_api.expect(page.locator("#leaderboard-body tr").first).to_contain_text("Eli")


def test_public_page_live_timer_runs(page_factory, backend):
    page = page_factory("/", api_key=None)
    sid = backend.http.post("/api/v1/session/start", json={"player_name": "Tim", "team": "Sales"}).json()["session_id"]
    sync_api.expect(page.locator("#live-banner")).to_be_visible()
    sync_api.expect(page.locator("#live-timer")).to_have_text("0.0")
    backend.http.post("/api/v1/game/run-start", json={"session_id": sid}).raise_for_status()
    page.wait_for_function("parseFloat(document.getElementById('live-timer').textContent) >= 0.5")


def test_streamlit_panel(backend, monkeypatch):
    testing = pytest.importorskip("streamlit.testing.v1")
    from conftest import ROOT
    monkeypatch.setenv("BACKEND_URL", backend.url)
    monkeypatch.setenv("GAME_API_KEY", API_KEY)
    at = testing.AppTest.from_file(str(ROOT / "frontend" / "app.py"), default_timeout=15).run()
    assert not at.exception
    assert at.title[0].value == "⚡ Buzz Wire Control Panel"
    at.text_input[0].input("Streamlit").run()
    next(b for b in at.button if b.label == "Start game").click().run()
    assert any("team" in e.value for e in at.error)           # team is required
    assert backend.http.get("/api/v1/session").json()["active"] is False
    next(s for s in at.selectbox if s.label == "Team / competency").select("Cybersecurity").run()
    next(b for b in at.button if b.label == "Start game").click().run()
    assert not at.exception and not at.error
    sess = backend.http.get("/api/v1/session").json()
    assert sess["active"] and sess["player_name"] == "Streamlit" and sess["team"] == "Cybersecurity"
    next(b for b in at.button if b.label == "Abort game").click().run()
    assert backend.http.get("/api/v1/session").json()["active"] is False
    assert os.environ["BACKEND_URL"] == backend.url
