"""Relay bench test. Run from Thonny (Run current script) - does not touch main.py.

Step 1: toy battery REMOVED. Listen for 5 clicks; confirm the multimeter beeps across
        the relay's COM and NO screws only while it is clicked on.
Step 2: only after step 1 passes, fit the toy's cells and test ONE pulse on your own hand.
Limited to 5 short pulses, 3 s apart.
"""
import time
from machine import Pin

import config

relay = Pin(config.SHOCK_PIN, Pin.OUT, value=config.RELAY_OFF)
PULSES = 5
PULSE_S = 0.15  # same as the game default; never exceed config.HARD_MAX_SHOCK_S

try:
    for i in range(PULSES):
        time.sleep(3)
        print("pulse %d/%d" % (i + 1, PULSES))
        relay.value(config.RELAY_ON)
        time.sleep(min(PULSE_S, config.HARD_MAX_SHOCK_S))
        relay.value(config.RELAY_OFF)
finally:
    relay.value(config.RELAY_OFF)
    print("done, relay off")
