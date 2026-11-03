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
DEBOUNCE_MS = 30          # track contact must be stable this long
HOOK_DEBOUNCE_MS = 80     # hooks swing when the wand is hung, so debounce longer
FAULT_LOCKOUT_MS = 1000   # after a fault, ignore further contacts for this long
MIN_RUN_MS = 1500         # ignore an end-hook contact in the first 1.5 s of a run
CONFIG_POLL_S = 30        # refresh the score formula from the backend
SESSION_POLL_S = 1        # check whether a game was started/aborted

# ---- Score (console preview only; the backend owns the official score) ------- #
# score = SCORE_BASE - SCORE_PER_SECOND * seconds - SCORE_PER_FAULT * faults, minimum 0
# These are overwritten with the backend's values once it is reachable.
SCORE_BASE = 1000
SCORE_PER_SECOND = 5
SCORE_PER_FAULT = 50
