"""Wi-Fi + minimal async HTTP/JSON client for MicroPython."""
import json
import gc
import time

import network
import uasyncio as asyncio

import config

wlan = network.WLAN(network.STA_IF)


def connect_wifi(timeout_s=20):
    """Blocking connect used at boot. Returns True when connected."""
    wlan.active(True)
    if wlan.isconnected():
        return True
    try:
        wlan.connect(config.WIFI_SSID, config.WIFI_PASSWORD)
    except OSError as exc:
        print("wifi connect error:", exc)
        return False
    deadline = time.ticks_add(time.ticks_ms(), timeout_s * 1000)
    while not wlan.isconnected() and time.ticks_diff(deadline, time.ticks_ms()) > 0:
        time.sleep_ms(250)
    if wlan.isconnected():
        print("wifi ok:", wlan.ifconfig()[0])
    return wlan.isconnected()


async def ensure_wifi(timeout_s=10):
    """Non-blocking (re)connect used while the game is running."""
    if wlan.isconnected():
        return True
    try:
        wlan.active(True)
        wlan.connect(config.WIFI_SSID, config.WIFI_PASSWORD)
    except OSError as exc:
        print("wifi reconnect error:", exc)
        return False
    for _ in range(timeout_s * 4):
        if wlan.isconnected():
            return True
        await asyncio.sleep_ms(250)
    return wlan.isconnected()


async def _exchange(method, path, body):
    payload = json.dumps(body) if body is not None else ""
    head = "%s %s HTTP/1.1\r\nHost: %s:%d\r\nConnection: close\r\nAccept: application/json\r\n" % (
        method, path, config.BACKEND_HOST, config.BACKEND_PORT)
    if config.API_KEY:
        head += "X-API-Key: %s\r\n" % config.API_KEY
    if body is not None:
        head += "Content-Type: application/json\r\nContent-Length: %d\r\n" % len(payload)
    request = (head + "\r\n" + payload).encode()

    writer = None
    try:
        reader, writer = await asyncio.open_connection(config.BACKEND_HOST, config.BACKEND_PORT)
        writer.write(request)
        await writer.drain()

        status = int((await reader.readline()).split()[1])
        length = None
        while True:
            line = await reader.readline()
            if line in (b"", b"\r\n", b"\n"):
                break
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":")[1])

        if length is None:
            data = await reader.read(-1)
        else:
            data = b""
            while len(data) < length:
                chunk = await reader.read(length - len(data))
                if not chunk:
                    break
                data += chunk
        return status, (json.loads(data) if data else None)
    finally:
        if writer is not None:
            writer.close()


async def request(method, path, body=None):
    """Return (status, json) or (None, None) on any network/parse failure. Never raises."""
    try:
        gc.collect()
        if not await ensure_wifi():
            return None, None
        return await asyncio.wait_for(_exchange(method, path, body), config.HTTP_TIMEOUT_S)
    except Exception as exc:  # timeouts, resets, bad JSON, DNS, ...
        print("http %s %s failed: %r" % (method, path, exc))
        return None, None
    finally:
        gc.collect()
