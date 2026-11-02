"""Wiring check. Run from Thonny BEFORE the game firmware (Run current script).

Watches the track and both hooks for 60 s and prints every change. Beeps when the wand
touches the track. The relay is held OFF the whole time, so this can never shock anyone.

Expected, with the wand (GND) in your hand:
  touch track      -> "TRACK: wand touching" + beep      release -> "open"
  hang on START    -> "START HOOK: wand touching"        lift    -> "open"
  hang on END      -> "END HOOK: wand touching"          lift    -> "open"
"""
import time
from machine import Pin, PWM

import config

relay = Pin(config.SHOCK_PIN, Pin.OUT, value=config.RELAY_OFF)  # held OFF

if config.BUZZER_ACTIVE:
    buz = Pin(config.BUZZER_PIN, Pin.OUT, value=0)
else:
    buz = PWM(Pin(config.BUZZER_PIN))
    buz.freq(2000)
    buz.duty_u16(0)


def buzz(on):
    if config.BUZZER_ACTIVE:
        buz.value(1 if on else 0)
    else:
        buz.duty_u16(32768 if on else 0)


def beep(ms=150):
    buzz(True)
    time.sleep_ms(ms)
    buzz(False)


inputs = (
    ("TRACK", Pin(config.TRACK_PIN, Pin.IN, Pin.PULL_UP)),
    ("START HOOK", Pin(config.START_HOOK_PIN, Pin.IN, Pin.PULL_UP)),
    ("END HOOK", Pin(config.END_HOOK_PIN, Pin.IN, Pin.PULL_UP)),
)

print("buzzer test: one beep")
beep(300)
print("watching inputs for 60 s ...")
last = [None, None, None]
stop = time.ticks_add(time.ticks_ms(), 60000)
try:
    while time.ticks_diff(stop, time.ticks_ms()) > 0:
        for i, (name, pin) in enumerate(inputs):
            touching = pin.value() == 0
            if touching != last[i]:
                last[i] = touching
                print("%s: %s" % (name, "wand touching" if touching else "open"))
                if name == "TRACK" and touching:
                    beep()
        time.sleep_ms(10)
finally:
    buzz(False)
    relay.value(config.RELAY_OFF)
    print("done")
