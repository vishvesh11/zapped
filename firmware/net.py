"""Wi-Fi + minimal async HTTP(S)/JSON client for MicroPython."""
import json
import gc
import time

import network
import uasyncio as asyncio

import config

wlan = network.WLAN(network.STA_IF)


def parse_backend(host, port):
    """Accept "host", "http://host:port", "https://host[/]" -> (hostname, port, use_tls)."""
    host = host.strip()
    tls = None
    if host.startswith("https://"):
        tls, host = True, host[8:]
    elif host.startswith("http://"):
        tls, host = False, host[7:]
    host = host.split("/", 1)[0]
    if ":" in host:
        host, p = host.rsplit(":", 1)
        port = int(p)
    elif tls is True and port in (None, 80):
        port = 443
    elif tls is False and port in (None, 443):
        port = 80
    if tls is None:
        tls = port == 443
    return host, int(port), tls


HOST, PORT, USE_TLS = parse_backend(config.BACKEND_HOST, config.BACKEND_PORT)
HOST_HEADER = HOST if PORT == (443 if USE_TLS else 80) else "%s:%d" % (HOST, PORT)

# One request at a time: a TLS handshake needs tens of KB of RAM on the Pico W, so
# overlapping session polls / touch reports could otherwise exhaust memory.
_lock = asyncio.Lock()


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


async def _read_exact(reader, n):
    data = b""
    while len(data) < n:
        chunk = await reader.read(n - len(data))
        if not chunk:
            break
        data += chunk
    return data


async def _read_chunked(reader):
    data = b""
    while True:
        size_line = await reader.readline()
        if not size_line:
            break
        size_txt = size_line.split(b";")[0].strip().decode()
        size = int(size_txt, 16) if size_txt else 0
        if size == 0:
            break
        data += await _read_exact(reader, size)
        await reader.readline()  # CRLF after each chunk
    return data


async def _exchange(method, path, body):
    payload = json.dumps(body) if body is not None else ""
    head = ("%s %s HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n"
            "Accept: application/json\r\nUser-Agent: buzzwire-pico/1.0\r\n") % (
        method, path, HOST_HEADER)
    if config.API_KEY:
        head += "X-API-Key: %s\r\n" % config.API_KEY
    if body is not None:
        head += "Content-Type: application/json\r\nContent-Length: %d\r\n" % len(payload)
    request = (head + "\r\n" + payload).encode()

    writer = None
    try:
        if USE_TLS:
            # MicroPython >= 1.22: ssl=True wraps the socket (no certificate verification).
            reader, writer = await asyncio.open_connection(HOST, PORT, ssl=True, server_hostname=HOST)
        else:
            reader, writer = await asyncio.open_connection(HOST, PORT)
        writer.write(request)
        await writer.drain()

        status = int((await reader.readline()).split()[1].decode())
        length = None
        chunked = False
        while True:
            line = await reader.readline()
            if line in (b"", b"\r\n", b"\n"):
                break
            lower = line.lower()
            if lower.startswith(b"content-length:"):
                length = int(line.split(b":")[1].strip().decode())
            elif lower.startswith(b"transfer-encoding:") and b"chunked" in lower:
                chunked = True

        if chunked:
            data = await _read_chunked(reader)
        elif length is None:
            data = await reader.read(-1)
        else:
            data = await _read_exact(reader, length)
        try:
            parsed = json.loads(data) if data else None
        except ValueError:
            parsed = None  # e.g. an HTML error page from a proxy; the status still counts
        return status, parsed
    finally:
        if writer is not None:
            writer.close()


async def request(method, path, body=None):
    """Return (status, json) or (None, None) on any network/parse failure. Never raises."""
    async with _lock:
        try:
            gc.collect()
            if not await ensure_wifi():
                return None, None
            return await asyncio.wait_for(_exchange(method, path, body), config.HTTP_TIMEOUT_S)
        except Exception as exc:  # timeouts, resets, DNS, TLS, ...
            print("http %s %s failed: %r" % (method, path, exc))
            return None, None
        finally:
            gc.collect()
