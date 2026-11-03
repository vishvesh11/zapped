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
    sync_api.expect(page.locator("#flash")).to_have_text("Game started.")
    sync_api.expect(page.locator("#st-player")).to_have_text("Alice")
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
    page.click("text=Start game")
    sync_api.expect(page.locator("#st-player")).to_have_text("Bob")
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
    assert not at.exception and not at.error
    sess = backend.http.get("/api/v1/session").json()
    assert sess["active"] and sess["player_name"] == "Streamlit"
    next(b for b in at.button if b.label == "Abort game").click().run()
    assert backend.http.get("/api/v1/session").json()["active"] is False
    assert os.environ["BACKEND_URL"] == backend.url
