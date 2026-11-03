"""Run the real MicroPython firmware under CPython.

Provides fake ``machine``, ``network`` and ``uasyncio`` modules plus MicroPython's
``time.ticks_*`` helpers, then imports ``firmware/config.py``, ``net.py`` and
``main.py`` unmodified. Pins are simulated: set ``FakePin.levels[gpio] = 0`` to
pull an input LOW (wand touching), ``1`` to release it.
"""
import asyncio
import contextlib
import importlib
import sys
import time
import types

from conftest import FIRMWARE_DIR

FIRMWARE_MODULES = ("config", "net", "main", "boot", "machine", "network", "uasyncio")


class _Levels(dict):
    """Input levels; a 1 -> 0 transition fires any falling-edge IRQ handler, like hardware."""

    def __setitem__(self, pin, level):
        old = self.get(pin, 1)
        super().__setitem__(pin, level)
        if old and not level and pin in FakePin.irqs:
            handler, fake_pin = FakePin.irqs[pin]
            handler(fake_pin)

    def clear(self):
        super().clear()
        FakePin.irqs.clear()


class FakePin:
    IN, OUT, PULL_UP = 0, 1, 2
    IRQ_FALLING, IRQ_RISING = 4, 8
    levels = _Levels()  # gpio -> input level (1 = released / pulled up, 0 = wand touching)
    outputs = {}        # gpio/name -> last written output value
    irqs = {}           # gpio -> (handler, pin) for falling-edge interrupts

    def __init__(self, pin, mode=None, pull=None, value=None):
        self.id = pin
        if mode == FakePin.OUT:
            FakePin.outputs[pin] = value or 0
        elif mode == FakePin.IN:
            FakePin.levels.setdefault(pin, 1)

    def irq(self, handler=None, trigger=None, hard=False):
        if trigger & FakePin.IRQ_FALLING:
            FakePin.irqs[self.id] = (handler, self)

    def value(self, v=None):
        if v is None:
            return FakePin.levels.get(self.id, 1)
        FakePin.outputs[self.id] = v


class FakePWM:
    def __init__(self, pin):
        self.pin = pin

    def freq(self, f):
        pass

    def duty_u16(self, d):
        FakePin.outputs[("pwm", self.pin.id)] = d


class FakeWDT:
    def __init__(self, timeout):
        self.timeout = timeout

    def feed(self):
        pass


class FakeWLAN:
    def __init__(self, iface):
        pass

    def active(self, *a):
        return True

    def isconnected(self):
        return True

    def connect(self, ssid, pw):
        pass

    def ifconfig(self):
        return ("127.0.0.1",)


def _fake_modules():
    machine = types.ModuleType("machine")
    machine.Pin, machine.PWM, machine.WDT = FakePin, FakePWM, FakeWDT
    network = types.ModuleType("network")
    network.WLAN, network.STA_IF = FakeWLAN, 0
    uasyncio = types.ModuleType("uasyncio")
    for name in dir(asyncio):
        if not name.startswith("_"):
            setattr(uasyncio, name, getattr(asyncio, name))
    uasyncio.sleep_ms = lambda ms: asyncio.sleep(ms / 1000)
    # main.py ends with asyncio.run(main()); at import time we only want the definitions.
    uasyncio.run = lambda coro: coro.close()
    uasyncio.new_event_loop = lambda: None
    return {"machine": machine, "network": network, "uasyncio": uasyncio}


def _install_ticks():
    start = time.monotonic()
    time.ticks_ms = lambda: int((time.monotonic() - start) * 1000)
    time.ticks_diff = lambda a, b: a - b
    time.ticks_add = lambda a, b: a + b
    time.sleep_ms = lambda ms: time.sleep(ms / 1000)


@contextlib.contextmanager
def load_firmware(**overrides):
    """Import the firmware with config overrides; restores sys.modules afterwards."""
    saved = {k: sys.modules.pop(k) for k in FIRMWARE_MODULES if k in sys.modules}
    FakePin.levels.clear()
    FakePin.outputs.clear()
    _install_ticks()
    sys.modules.update(_fake_modules())
    sys.path.insert(0, str(FIRMWARE_DIR))
    try:
        config = importlib.import_module("config")
        for k, v in overrides.items():
            assert hasattr(config, k), f"unknown firmware config {k}"
            setattr(config, k, v)
        net = importlib.import_module("net")
        main = importlib.import_module("main")
        yield types.SimpleNamespace(config=config, net=net, main=main)
    finally:
        sys.path.remove(str(FIRMWARE_DIR))
        for k in FIRMWARE_MODULES:
            sys.modules.pop(k, None)
        sys.modules.update(saved)


async def wait_for(predicate, timeout=5.0, interval=0.02, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return result
        await asyncio.sleep(interval)
    raise AssertionError(f"timed out waiting for {what}")
