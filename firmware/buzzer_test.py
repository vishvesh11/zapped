"""Standalone buzzer and LED test. Run this script on the Pico with MicroPython."""
import time
from machine import Pin, PWM

import config

# Initialize the onboard LED
led = Pin("LED", Pin.OUT)

if config.BUZZER_ACTIVE:
    buzzer = Pin(config.BUZZER_PIN, Pin.OUT, value=0)
else:
    buzzer = PWM(Pin(config.BUZZER_PIN))
    buzzer.freq(2000)
    buzzer.duty_u16(0)


def set_buzzer_and_led(on):
    # Control the onboard LED
    led.value(1 if on else 0)
    
    # Control the buzzer
    if config.BUZZER_ACTIVE:
        buzzer.value(1 if on else 0)
    else:
        buzzer.duty_u16(32768 if on else 0)


try:
    print("Buzzer and LED test: 3 beeps & flashes")
    for _ in range(3):
        set_buzzer_and_led(True)   # Turn both ON
        time.sleep_ms(200)
        set_buzzer_and_led(False)  # Turn both OFF
        time.sleep_ms(200)
    print("Test complete")
finally:
    set_buzzer_and_led(False)       # always leave both off
