"""Runs once at power-up: force the shock output low, then join Wi-Fi (live mode only)."""
from machine import Pin

import config

Pin(config.SHOCK_PIN, Pin.OUT, value=config.RELAY_OFF)  # make sure the shock output is OFF first

if config.BENCH_MODE:
    print("boot: BENCH MODE - no Wi-Fi, shock output disabled")
else:
    import net
    if not net.connect_wifi():
        print("boot: wifi not connected yet; main.py will keep retrying")
