"""Buzz-wire game firmware for Raspberry Pi Pico W (MicroPython).

The wand is wired to GND. Game flow:

  IDLE     No game armed. Track touches are ignored, so the wand can be carried back
           to the start freely.
  READY    "Start" was clicked. Wand hangs on the START hook, timer is at 0.
  RUNNING  Wand lifted off the START hook: timer runs, every track touch is a fault
           (local beep + reported to the backend, which plays the chime and snaps a photo).
  Hanging the wand on the END hook finishes the game, reports the result and
  returns to IDLE until the next "Start" click.
"""
import time

import uasyncio as asyncio
from machine import Pin, PWM, WDT

import config
import net

IDLE, READY, RUNNING = 0, 1, 2
PHASE_NAMES = ("IDLE", "READY", "RUNNING")

led = Pin("LED", Pin.OUT)

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


class TrackSensor:
    """Wand-on-track detector tuned not to miss brief brushes.

    A brush can be shorter than one scan, and the event loop stalls for a while during
    DNS/TLS work. A hard IRQ on the falling edge latches every contact regardless, and
    the game loop also looks at the live pin level for contacts that are being held.
    """

    def __init__(self, pin_no):
        self.pin = Pin(pin_no, Pin.IN, Pin.PULL_UP)
        self._hit = False
        if config.TRACK_IRQ:
            try:
                self.pin.irq(handler=self._irq, trigger=Pin.IRQ_FALLING, hard=True)
            except Exception as exc:
                log("track IRQ unavailable (%r); using polling only" % exc)

    def _irq(self, _pin):
        self._hit = True  # hard IRQ: no allocation allowed, just set a flag

    def clear(self):
        self._hit = False

    def contact(self):
        """True if the wand touched the track since the last call or is touching it now."""
        hit = self._hit
        self._hit = False
        return hit or self.pin.value() == 0


class TouchCounter:
    """Turns raw track contact into faults.

    - A new touch starts once the wand has been off the track for TRACK_RELEASE_MS, so
      contact chatter while scraping counts once.
    - Faults are at least FAULT_LOCKOUT_MS apart; a new touch inside that gap is owed and
      counted when it ends instead of being dropped.
    - Holding/dragging along the track adds a fault every HOLD_REPEAT_MS.
    """

    def __init__(self):
        self.reset(time.ticks_ms())

    def reset(self, now):
        self.touching = False
        self.owed = False
        self.last_contact = now
        self.last_fault = time.ticks_add(now, -config.FAULT_LOCKOUT_MS)
        self.hold_since = now

    def update(self, contact, now):
        """Return True when a fault should be counted on this scan."""
        if contact:
            self.last_contact = now
            if not self.touching:
                self.touching = True
                self.hold_since = now
                self.owed = True
        elif self.touching and time.ticks_diff(now, self.last_contact) >= config.TRACK_RELEASE_MS:
            self.touching = False

        if (config.HOLD_REPEAT_MS and self.touching and not self.owed
                and time.ticks_diff(now, self.hold_since) >= config.HOLD_REPEAT_MS):
            self.owed = True

        if self.owed and time.ticks_diff(now, self.last_fault) >= config.FAULT_LOCKOUT_MS:
            self.owed = False
            self.last_fault = now
            self.hold_since = now
            return True
        return False


track = TrackSensor(config.TRACK_PIN)
touches = TouchCounter()
start_hook = StableInput(config.START_HOOK_PIN, config.HOOK_DEBOUNCE_MS)
end_hook = StableInput(config.END_HOOK_PIN, config.HOOK_DEBOUNCE_MS)


class State:
    def __init__(self):
        self.phase = IDLE
        self.session_id = None
        self.backend_failures = 0
        self.hook_seen = False              # wand has rested on the START hook this game
        self.hint_shown = False
        self.run_start_ms = 0
        self.faults = 0
        self.touch_busy = False
        self.pending = None                 # finished result waiting to be reported
        self.sending = False
        self.beeping = False


state = State()


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


def preview_score(seconds, faults):
    return max(0, int(round(config.SCORE_BASE - config.SCORE_PER_SECOND * seconds
                            - config.SCORE_PER_FAULT * faults)))


# --------------------------------------------------------------------------- #
# Backend access (real, or simulated in bench mode)
# --------------------------------------------------------------------------- #
bench = {"active": False, "id": None, "n": 0, "since": None}


def bench_call(path, body):
    now = time.ticks_ms()
    if path.endswith("/config"):
        return 200, {"score_base": config.SCORE_BASE,
                     "score_per_second": config.SCORE_PER_SECOND,
                     "score_per_fault": config.SCORE_PER_FAULT}
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
        return 200, {"chime_queued": True, "running": True,
                     "touch_count": (body or {}).get("fault_count", 0)}
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


async def fetch_config():
    """Sync the score formula (used for the console preview) with the backend."""
    status, data = await call("GET", "/api/v1/config")
    ok = status == 200 and isinstance(data, dict)
    if ok:
        for key, attr in (("score_base", "SCORE_BASE"),
                          ("score_per_second", "SCORE_PER_SECOND"),
                          ("score_per_fault", "SCORE_PER_FAULT")):
            if isinstance(data.get(key), (int, float)):
                setattr(config, attr, data[key])
    elif status == 401:
        log("backend rejected API_KEY (401): check config.API_KEY")
    note_backend(ok)
    return ok


async def init_config():
    for _ in range(5):
        if await fetch_config():
            log("backend reachable at %s:%d (tls=%s)" % (net.HOST, net.PORT, net.USE_TLS)
                if not config.BENCH_MODE else "bench backend ready")
            return
        await asyncio.sleep(2)
    log("backend unreachable at start; will keep retrying")


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
            log("ARMED (session %s). Hang the wand on the START hook, then lift it to begin." % sid)
    else:
        if state.phase != IDLE:
            log("game aborted by control panel -> IDLE")
        state.phase = IDLE
        state.session_id = None


def begin_run():
    state.phase = RUNNING
    state.run_start_ms = time.ticks_ms()
    state.faults = 0
    track.clear()           # brushes while carrying the wand to START don't count
    touches.reset(state.run_start_ms)
    log("RUN STARTED - timer running")
    asyncio.create_task(beep(1, 80, 1500))
    asyncio.create_task(report_run_start())


async def report_run_start():
    sid = state.session_id
    for _ in range(3):
        if state.session_id != sid:
            return  # game was aborted/replaced meanwhile
        status, _data = await call("POST", "/api/v1/game/run-start", {
            "session_id": sid, "device_id": config.DEVICE_ID})
        if status == 200:
            note_backend(True)
            return
        if status == 409:
            return  # session no longer exists on the backend
        note_backend(False)
        await asyncio.sleep(1)
    log("could not tell backend the run started (timer still runs locally)")


def count_fault():
    """Count a fault synchronously (so it can't be lost to a same-scan finish) and report it."""
    state.faults += 1
    log("FAULT #%d" % state.faults)
    asyncio.create_task(beep(1, 150, 2000))
    if not state.touch_busy:
        asyncio.create_task(report_touches(state.session_id))


async def report_touches(sid):
    """Tell the backend about touches (chime + photo). Faults that arrive while a report is
    in flight are sent in a follow-up report; the final result carries the exact count anyway."""
    state.touch_busy = True
    reported = 0
    try:
        while state.phase == RUNNING and state.session_id == sid and state.faults > reported:
            reported = state.faults
            status, data = await call("POST", "/api/v1/event/touch", {
                "device_id": config.DEVICE_ID,
                "session_id": sid,
                "fault_count": reported,
                "uptime_ms": time.ticks_ms(),
            })
            note_backend(status == 200 and isinstance(data, dict))
    except Exception as exc:
        print("report_touches:", repr(exc))
    finally:
        state.touch_busy = False


def finish_run():
    elapsed = time.ticks_diff(time.ticks_ms(), state.run_start_ms) / 1000
    state.phase = IDLE      # stop counting faults immediately
    state.pending = {
        "session_id": state.session_id,
        "touch_count": state.faults,
        "completion_time_seconds": max(elapsed, 0.01),
    }
    log("FINISHED in %.2fs with %d faults (preview score %d). Carry the wand back."
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
            rank = data.get("rank") if isinstance(data, dict) else None
            log("result reported%s" % (" (rank #%s)" % rank if rank else ""))
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
    while True:
        try:
            contact = track.contact()
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
                if touches.update(contact, now):
                    count_fault()
                if end_hook.engaged and time.ticks_diff(now, state.run_start_ms) >= config.MIN_RUN_MS:
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
        await asyncio.sleep(config.SESSION_POLL_RUNNING_S if state.phase == RUNNING
                            else config.SESSION_POLL_S)


async def watchdog_task(wdt):
    while True:
        wdt.feed()
        await asyncio.sleep_ms(1000)


async def main():
    buzzer_set(False)
    if config.BENCH_MODE:
        log("BENCH MODE: no backend. Hang the wand on the START hook for 2 s to arm a game.")
    else:
        await net.ensure_wifi()
    if config.USE_WATCHDOG:
        # Start feeding before anything that can block for long (init_config retries ~60 s).
        asyncio.create_task(watchdog_task(WDT(timeout=8000)))  # reboots the board if the loop hangs
    await init_config()
    await asyncio.gather(session_task(), game_task(), config_task())


try:
    asyncio.run(main())
finally:
    buzzer_set(False)
    asyncio.new_event_loop()
