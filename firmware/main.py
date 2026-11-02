"""Buzz-wire game firmware for Raspberry Pi Pico W (MicroPython).

The wand is wired to GND. Game flow:

  IDLE     No game armed. Track touches are ignored and the shock output is locked off,
           so the wand can be carried back to the start freely.
  READY    "Start" was clicked. Wand hangs on the START hook, timer is at 0.
  RUNNING  Wand lifted off the START hook: timer runs, every track touch is a fault.
  Hanging the wand on the END hook finishes the game, reports the result and
  returns to IDLE (disarmed) until the next "Start" click.
"""
import time

import uasyncio as asyncio
from machine import Pin, PWM, WDT

import config
import net

IDLE, READY, RUNNING = 0, 1, 2
PHASE_NAMES = ("IDLE", "READY", "RUNNING")

led = Pin("LED", Pin.OUT)
shock_pin = Pin(config.SHOCK_PIN, Pin.OUT, value=config.RELAY_OFF)

if config.BUZZER_ACTIVE:
    _buzzer = Pin(config.BUZZER_PIN, Pin.OUT, value=0)
else:
    _buzzer = PWM(Pin(config.BUZZER_PIN))
    _buzzer.freq(2000)
    _buzzer.duty_u16(0)


def log(msg):
    print("[%8.2f] %s" % (time.ticks_ms() / 1000, msg))


class StableInput:
    """Debounced active-low input. `engaged` is True while the pin is stably LOW.
    update() returns True on the loop iteration where `engaged` changed."""

    def __init__(self, pin_no, debounce_ms):
        self.pin = Pin(pin_no, Pin.IN, Pin.PULL_UP)
        self.debounce_ms = debounce_ms
        self.engaged = self.pin.value() == 0
        self._candidate = self.engaged
        self._since = time.ticks_ms()

    def update(self):
        raw = self.pin.value() == 0
        now = time.ticks_ms()
        if raw != self._candidate:
            self._candidate = raw
            self._since = now
        elif raw != self.engaged and time.ticks_diff(now, self._since) >= self.debounce_ms:
            self.engaged = raw
            return True
        return False


track = StableInput(config.TRACK_PIN, config.DEBOUNCE_MS)
start_hook = StableInput(config.START_HOOK_PIN, config.HOOK_DEBOUNCE_MS)
end_hook = StableInput(config.END_HOOK_PIN, config.HOOK_DEBOUNCE_MS)


class State:
    def __init__(self):
        self.phase = IDLE
        self.session_id = None
        self.shock_enabled = False          # fail-safe default
        self.shock_duration = config.DEFAULT_SHOCK_S
        self.backend_failures = 0
        self.hook_seen = False              # wand has rested on the START hook this game
        self.hint_shown = False
        self.run_start_ms = 0
        self.faults = 0
        self.shocks_fired = 0
        self.last_shock_ms = None
        self.touch_busy = False
        self.pending = None                 # finished result waiting to be reported
        self.sending = False
        self.beeping = False


state = State()


def set_output(value):
    """Close/open the relay that replaces the toy's switch contact. In bench mode only the LED is used."""
    if config.BENCH_MODE:
        led.value(value)
    else:
        shock_pin.value(config.RELAY_ON if value else config.RELAY_OFF)


def buzzer_set(on, freq=2000):
    if config.BUZZER_ACTIVE:
        _buzzer.value(1 if on else 0)
    elif on:
        _buzzer.freq(freq)
        _buzzer.duty_u16(32768)
    else:
        _buzzer.duty_u16(0)


async def beep(times=1, ms=120, freq=2000):
    """Short non-blocking beeps. Skipped if another beep is already playing."""
    if not config.BUZZER_ENABLED or state.beeping:
        return
    state.beeping = True
    try:
        for i in range(times):
            buzzer_set(True, freq)
            await asyncio.sleep_ms(ms)
            buzzer_set(False)
            if i < times - 1:
                await asyncio.sleep_ms(ms)
    finally:
        buzzer_set(False)
        state.beeping = False


def clamp_duration(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = config.DEFAULT_SHOCK_S
    return max(config.MIN_SHOCK_S, min(value, config.HARD_MAX_SHOCK_S))


def preview_score(seconds, faults):
    return max(0, int(config.SCORE_BASE - config.SCORE_PER_SECOND * seconds
                      - config.SCORE_PER_FAULT * faults))


# --------------------------------------------------------------------------- #
# Backend access (real, or simulated in bench mode)
# --------------------------------------------------------------------------- #
bench = {"active": False, "id": None, "n": 0, "since": None}


def bench_call(path, body):
    now = time.ticks_ms()
    if path.endswith("/config"):
        return 200, {"shock_enabled": True, "shock_duration": config.DEFAULT_SHOCK_S}
    if path.endswith("/session"):
        if not bench["active"]:
            if start_hook.engaged:
                if bench["since"] is None:
                    bench["since"] = now
                elif time.ticks_diff(now, bench["since"]) >= 2000:
                    bench["n"] += 1
                    bench["id"] = "bench-%d" % bench["n"]
                    bench["active"] = True
                    log("[bench] simulated START click")
            else:
                bench["since"] = None
        return 200, {"active": bench["active"], "session_id": bench["id"]}
    if path.endswith("/event/touch"):
        return 200, {"shock": True, "shock_duration": state.shock_duration}
    if path.endswith("/run-start"):
        return 200, {"ok": True}
    if path.endswith("/game/complete"):
        bench["active"] = False
        bench["since"] = None
        return 200, {"rank": 1}
    return 404, None


async def call(method, path, body=None):
    if config.BENCH_MODE:
        return bench_call(path, body)
    return await net.request(method, path, body)


def note_backend(ok):
    state.backend_failures = 0 if ok else state.backend_failures + 1
    if not config.BENCH_MODE:
        led.value(1 if ok else 0)  # LED on = backend reachable
        if state.backend_failures >= 3 and state.shock_enabled:
            state.shock_enabled = False
            log("backend lost: shock disarmed locally")


async def fetch_config():
    status, data = await call("GET", "/api/v1/config")
    ok = status == 200 and isinstance(data, dict)
    if ok:
        state.shock_enabled = bool(data.get("shock_enabled"))
        state.shock_duration = clamp_duration(data.get("shock_duration"))
    note_backend(ok)
    return ok


async def init_config():
    for _ in range(5):
        if await fetch_config():
            log("config loaded: shock=%s duration=%.2fs" % (state.shock_enabled, state.shock_duration))
            return
        await asyncio.sleep(2)
    log("backend unreachable at start; running with shock disabled")


# --------------------------------------------------------------------------- #
# Shock output (all local safety limits live here)
# --------------------------------------------------------------------------- #
async def fire_shock(duration):
    if state.phase != RUNNING:
        return False
    if state.shocks_fired >= config.MAX_SHOCKS_PER_SESSION:
        log("shock skipped: per-game cap reached")
        return False
    now = time.ticks_ms()
    if (state.last_shock_ms is not None
            and time.ticks_diff(now, state.last_shock_ms) < config.SHOCK_COOLDOWN_MS):
        log("shock skipped: cooldown")
        return False

    state.last_shock_ms = now
    state.shocks_fired += 1
    total_ms = int(clamp_duration(duration) * 1000)
    log("SHOCK %d ms%s" % (total_ms, " (bench: LED only)" if config.BENCH_MODE else ""))
    waited = 0
    try:
        set_output(1)
        while waited < total_ms and state.phase == RUNNING:  # stops early if the game ends
            await asyncio.sleep_ms(10)
            waited += 10
    finally:
        set_output(0)  # always released, even on error/cancel
    return True


# --------------------------------------------------------------------------- #
# Game logic
# --------------------------------------------------------------------------- #
def apply_session(data):
    active = bool(data.get("active"))
    sid = data.get("session_id")
    if active:
        if sid != state.session_id:  # a new game was started from the control panel
            state.session_id = sid
            state.phase = READY
            state.hook_seen = start_hook.engaged
            state.hint_shown = False
            state.faults = 0
            state.shocks_fired = 0
            state.last_shock_ms = None
            set_output(0)
            log("ARMED (session %s). Hang the wand on the START hook, then lift it to begin." % sid)
    else:
        if state.phase != IDLE:
            log("game aborted by control panel -> IDLE")
        state.phase = IDLE
        state.session_id = None
        set_output(0)


def begin_run():
    state.phase = RUNNING
    state.run_start_ms = time.ticks_ms()
    state.faults = 0
    state.shocks_fired = 0
    state.last_shock_ms = None
    log("RUN STARTED - timer running")
    asyncio.create_task(beep(1, 80, 1500))
    asyncio.create_task(report_run_start())


async def report_run_start():
    sid = state.session_id
    for _ in range(3):
        status, _data = await call("POST", "/api/v1/game/run-start", {
            "session_id": sid, "device_id": config.DEVICE_ID})
        if status == 200:
            note_backend(True)
            return
        note_backend(False)
        await asyncio.sleep(1)
    log("could not tell backend the run started (timer still runs locally)")


async def handle_fault():
    if state.phase != RUNNING:
        return
    state.faults += 1
    log("FAULT #%d" % state.faults)
    asyncio.create_task(beep(1, 150, 2000))
    if state.touch_busy:
        return
    state.touch_busy = True
    try:
        status, data = await call("POST", "/api/v1/event/touch", {
            "device_id": config.DEVICE_ID,
            "session_id": state.session_id,
            "fault_count": state.faults,
            "uptime_ms": time.ticks_ms(),
        })
        if status == 200 and isinstance(data, dict):
            note_backend(True)
            if data.get("shock"):
                await fire_shock(data.get("shock_duration", state.shock_duration))
        else:
            note_backend(False)
            if config.ALLOW_CACHED_FALLBACK and state.shock_enabled:
                await fire_shock(state.shock_duration)
    except Exception as exc:
        print("handle_fault:", repr(exc))
    finally:
        state.touch_busy = False


def finish_run():
    elapsed = time.ticks_diff(time.ticks_ms(), state.run_start_ms) / 1000
    state.phase = IDLE      # disarm immediately: no more faults or shocks
    set_output(0)
    state.pending = {
        "session_id": state.session_id,
        "touch_count": state.faults,
        "completion_time_seconds": max(elapsed, 0.01),
    }
    log("FINISHED in %.2fs with %d faults (preview score %d). Disarmed - carry the wand back."
        % (elapsed, state.faults, preview_score(elapsed, state.faults)))
    asyncio.create_task(beep(2, 120, 1800))
    asyncio.create_task(send_pending())


async def send_pending():
    if state.sending or state.pending is None:
        return
    state.sending = True
    try:
        status, data = await call("POST", "/api/v1/game/complete", state.pending)
        if status == 200:
            note_backend(True)
            log("result reported")
            state.pending = None
        elif status in (409, 422):  # session gone / rejected: retrying will not help
            log("result rejected by backend (%s)" % status)
            state.pending = None
        else:
            note_backend(False)     # keep it and retry from session_task
    except Exception as exc:
        print("send_pending:", repr(exc))
    finally:
        state.sending = False


# --------------------------------------------------------------------------- #
# Tasks
# --------------------------------------------------------------------------- #
async def game_task():
    lockout_until = time.ticks_ms()
    while True:
        try:
            track_changed = track.update()
            start_hook.update()
            end_hook.update()

            if state.phase == READY:
                if start_hook.engaged:
                    if not state.hook_seen:
                        log("wand on START hook. Lift it to begin.")
                    state.hook_seen = True
                elif state.hook_seen:
                    begin_run()
                elif not state.hint_shown:
                    state.hint_shown = True
                    log("waiting for the wand to be hung on the START hook")

            elif state.phase == RUNNING:
                now = time.ticks_ms()
                if track_changed and track.engaged and time.ticks_diff(now, lockout_until) >= 0:
                    lockout_until = time.ticks_add(now, config.FAULT_LOCKOUT_MS)
                    asyncio.create_task(handle_fault())
                elif end_hook.engaged and time.ticks_diff(now, state.run_start_ms) >= config.MIN_RUN_MS:
                    finish_run()
        except Exception as exc:
            print("game_task:", repr(exc))
        await asyncio.sleep_ms(config.POLL_MS)


async def config_task():
    while True:
        await asyncio.sleep(config.CONFIG_POLL_S)
        try:
            await fetch_config()
        except Exception as exc:
            print("config_task:", repr(exc))


async def session_task():
    while True:
        try:
            status, data = await call("GET", "/api/v1/session")
            if status == 200 and isinstance(data, dict):
                apply_session(data)
                note_backend(True)
            else:
                note_backend(False)
            await send_pending()
        except Exception as exc:
            print("session_task:", repr(exc))
        await asyncio.sleep(config.SESSION_POLL_S)


async def watchdog_task(wdt):
    while True:
        wdt.feed()
        await asyncio.sleep_ms(1000)


async def main():
    set_output(0)
    shock_pin.value(config.RELAY_OFF)
    if config.BENCH_MODE:
        log("BENCH MODE: shock output disabled. Hang the wand on the START hook for 2 s to arm a game.")
    else:
        await net.ensure_wifi()
    await init_config()
    tasks = [config_task(), session_task(), game_task()]
    if config.USE_WATCHDOG:
        tasks.append(watchdog_task(WDT(timeout=8000)))  # reboots the board if the loop hangs
    await asyncio.gather(*tasks)


try:
    asyncio.run(main())
finally:
    shock_pin.value(config.RELAY_OFF)
    asyncio.new_event_loop()
