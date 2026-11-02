"""Pico W configuration. Edit before copying to the board."""

# ---- Mode ------------------------------------------------------------------ #
# True  = BENCH MODE: no Wi-Fi, no backend, shock output is NEVER driven (the onboard
#         LED blinks instead). A game auto-arms 2 s after the wand rests on the START
#         hook. Use this to verify wiring and game logic from the serial console.
# False = LIVE MODE: talks to the backend and can drive the shock output.
BENCH_MODE = False

# Watchdog reboots the board if the program ever hangs. It cannot be switched off once started,
# so it is only used in live mode (it would keep rebooting the board while you debug).
USE_WATCHDOG = not BENCH_MODE

# ---- Network (live mode) ---------------------------------------------------- #
WIFI_SSID = "fast_iot"
WIFI_PASSWORD = "9182736455"
BACKEND_HOST = "192.168.5.82"   # IP of the machine running the FastAPI backend
BACKEND_PORT = 8000
API_KEY = "changeme"            # must match GAME_API_KEY on the backend ("" = none)
DEVICE_ID = "pico-buzzwire-1"
HTTP_TIMEOUT_S = 10

# ---- Pins (GPIO numbers) ---------------------------------------------------- #
# The wand is wired to GND. Every input below uses the Pico's internal pull-up (3.3 V),
# so touching the wand to a contact pulls that pin LOW.
TRACK_PIN = 15       # copper track -> 1k resistor -> GP15
START_HOOK_PIN = 16  # start hook   -> 1k resistor -> GP16
END_HOOK_PIN = 17    # end hook     -> 1k resistor -> GP17
SHOCK_PIN = 14       # GP14 -> relay module IN (the relay closes the toy's own switch contact)

# ---- Relay module ------------------------------------------------------------ #
# Buy a module that says "high level trigger" (or has an H/L jumper set to H).
# If yours is active-LOW, set False. NOTE: an active-low module fires for about a
# second at every power-up/reset, so keep the toy's battery switched off until booted.
RELAY_ACTIVE_HIGH = True
RELAY_OFF = 0 if RELAY_ACTIVE_HIGH else 1
RELAY_ON = 1 - RELAY_OFF

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
CONFIG_POLL_S = 10        # refresh config from backend
SESSION_POLL_S = 1        # check whether a game was started/aborted

# ---- Shock safety (enforced locally, regardless of what the backend says) ---- #
DEFAULT_SHOCK_S = 0.15
MIN_SHOCK_S = 0.05
HARD_MAX_SHOCK_S = 0.5        # keep in sync with backend SHOCK_MAX_S
SHOCK_COOLDOWN_MS = 2000      # minimum gap between shocks
MAX_SHOCKS_PER_SESSION = 15
# If False, a fault never shocks unless the backend answered "shock": true for that fault.
ALLOW_CACHED_FALLBACK = False

# ---- Score (console preview only; the backend owns the official score) ------- #
# score = SCORE_BASE - SCORE_PER_SECOND * seconds - SCORE_PER_FAULT * faults, minimum 0
SCORE_BASE = 1000
SCORE_PER_SECOND = 5
SCORE_PER_FAULT = 50
