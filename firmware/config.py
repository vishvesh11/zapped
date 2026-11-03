"""Pico W configuration. Edit before copying to the board."""

# ---- Mode ------------------------------------------------------------------ #
# True  = BENCH MODE: no Wi-Fi, no backend. A game auto-arms 2 s after the wand rests on
#         the START hook. Use this to verify wiring and game logic from the serial console.
# False = LIVE MODE: talks to the backend.
BENCH_MODE = False

# Watchdog reboots the board if the program ever hangs. It cannot be switched off once started,
# so it is only used in live mode (it would keep rebooting the board while you debug).
USE_WATCHDOG = not BENCH_MODE

# ---- Network (live mode) ---------------------------------------------------- #
WIFI_SSID = "fast_iot"
WIFI_PASSWORD = "9182736455"
# Either a full URL ("https://diwaligame.vishvesh.me", "http://192.168.5.82:8000")
# or a bare host/IP plus BACKEND_PORT. https:// (or port 443) uses TLS, which needs
# MicroPython >= 1.22 on the Pico W.
BACKEND_HOST = "https://diwaligame.vishvesh.me"
BACKEND_PORT = 443              # ignored when BACKEND_HOST already contains a port
API_KEY = "changeme"            # must match GAME_API_KEY on the backend ("" = none)
DEVICE_ID = "pico-buzzwire-1"
HTTP_TIMEOUT_S = 10

# ---- Pins (GPIO numbers) ---------------------------------------------------- #
# The wand is wired to GND. Every input below uses the Pico's internal pull-up (3.3 V),
# so touching the wand to a contact pulls that pin LOW.
TRACK_PIN = 15       # copper track -> 1k resistor -> GP15
START_HOOK_PIN = 16  # start hook   -> 1k resistor -> GP16
END_HOOK_PIN = 17    # end hook     -> 1k resistor -> GP17

# ---- Buzzer (local feedback, works even if the backend is down) --------------- #
# GP13 -> 330R -> buzzer (+); buzzer (-) -> GND.
# ACTIVE buzzer = beeps by itself when given power. PASSIVE = needs a tone (PWM).
BUZZER_ENABLED = True
BUZZER_PIN = 13
BUZZER_ACTIVE = True

# ---- Timing ----------------------------------------------------------------- #
POLL_MS = 5               # input scan interval
HOOK_DEBOUNCE_MS = 80     # hooks swing when the wand is hung, so debounce longer
MIN_RUN_MS = 1500         # ignore an end-hook contact in the first 1.5 s of a run
CONFIG_POLL_S = 30        # refresh the score formula from the backend
SESSION_POLL_S = 1        # check whether a game was started/aborted
SESSION_POLL_RUNNING_S = 4  # poll less during a run: each HTTPS call stalls the Pico briefly

# ---- Touch sensitivity -------------------------------------------------------- #
# A hardware interrupt latches even a split-millisecond brush of the track, so touches are
# caught while the Pico is busy with Wi-Fi/TLS. The values below decide what counts as a
# *separate* touch, which keeps the game fun (no beep storms) without dropping real touches.
TRACK_IRQ = True          # latch brief brushes with a pin interrupt (False = polling only)
TRACK_RELEASE_MS = 120    # wand must be off the track this long before the next touch is new
FAULT_LOCKOUT_MS = 350    # minimum gap between two faults; a re-touch inside it is counted
                          # when the gap ends, never dropped
HOLD_REPEAT_MS = 1500     # dragging along the track adds a fault every 1.5 s (0 = off)

# ---- Score (console preview only; the backend owns the official score) ------- #
# score = SCORE_BASE - SCORE_PER_SECOND * seconds - SCORE_PER_FAULT * faults, minimum 0
# These are overwritten with the backend's values once it is reachable.
SCORE_BASE = 1000
SCORE_PER_SECOND = 5
SCORE_PER_FAULT = 50
