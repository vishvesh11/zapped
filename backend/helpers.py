"""Shared infrastructure for the Buzz-wire backend.

Holds the in-memory game state, the SSE broadcaster, media/camera helpers and
the SQLite leaderboard. Route handlers in ``routes.py`` build on these.
"""
import asyncio
import io
import json
import logging
import math
import sqlite3
import struct
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import boto3
from botocore.client import Config as BotoConfig
import httpx
from PIL import Image, ImageDraw, ImageFont

from config import settings
from schemas import ConfigResponse, LeaderboardEntry, SessionResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("buzzwire")

# --------------------------------------------------------------------------- #
# S3 client (Garage)
# --------------------------------------------------------------------------- #
s3_client = None
if settings.s3_access_key and settings.s3_secret_key:
    s3_client = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint,
        region_name=settings.s3_region,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def compute_score(seconds: float, faults: int) -> int:
    return max(
        0,
        int(round(
            settings.score_base
            - settings.score_per_second * seconds
            - settings.score_per_fault * faults
        )),
    )


# --------------------------------------------------------------------------- #
# SSE Event Broadcaster
# --------------------------------------------------------------------------- #
class EventBroadcaster:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: List[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=50)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def broadcast(self, event_type: str, data: dict) -> None:
        payload = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
        with self._lock:
            for q in list(self._subscribers):
                try:
                    q.put_nowait(payload)
                except asyncio.QueueFull:
                    pass


broadcaster = EventBroadcaster()


# --------------------------------------------------------------------------- #
# In-memory game state
# --------------------------------------------------------------------------- #
@dataclass
class Session:
    id: str
    player_name: str
    run_started_at: Optional[float] = None
    touch_count: int = 0
    shocks_fired: int = 0
    last_shock_at: Optional[float] = None

    @property
    def running(self) -> bool:
        return self.run_started_at is not None

    def elapsed(self) -> float:
        return time.monotonic() - self.run_started_at if self.running else 0.0


class GameState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.shock_enabled = False
        self.shock_duration = settings.shock_default_s
        self.media_file = settings.default_media
        self.session: Optional[Session] = None

    def config(self) -> ConfigResponse:
        return ConfigResponse(
            shock_enabled=self.shock_enabled,
            shock_duration=self.shock_duration,
            current_media_file=self.media_file,
        )


state = GameState()


def session_response() -> SessionResponse:
    s = state.session
    if s is None:
        return SessionResponse(active=False)
    elapsed = s.elapsed()
    return SessionResponse(
        active=True, session_id=s.id, player_name=s.player_name,
        phase="running" if s.running else "waiting",
        touch_count=s.touch_count, shocks_fired=s.shocks_fired,
        elapsed_seconds=round(elapsed, 2),
        live_score=compute_score(elapsed, s.touch_count) if s.running else int(settings.score_base),
    )


# --------------------------------------------------------------------------- #
# Media & Camera Helpers
# --------------------------------------------------------------------------- #
def resolve_media(name: str) -> Path:
    media_dir = settings.media_dir_resolved
    candidate = (media_dir / name).resolve()
    if candidate.parent != media_dir:
        raise ValueError("media file must be inside media folder")
    if candidate.suffix.lower() not in settings.allowed_ext:
        raise ValueError("unsupported type")
    if not candidate.is_file():
        raise ValueError(f"file not found: {name}")
    return candidate


def list_media() -> List[str]:
    media_dir = settings.media_dir_resolved
    if not media_dir.exists():
        return []
    return sorted(p.name for p in media_dir.iterdir()
                  if p.is_file() and p.suffix.lower() in settings.allowed_ext)


def ensure_default_chime() -> None:
    media_dir = settings.media_dir_resolved
    media_dir.mkdir(parents=True, exist_ok=True)
    target = media_dir / settings.default_media
    if target.exists():
        return
    rate, frames = 22050, bytearray()
    for freq in (880, 1320):
        n = int(rate * 0.18)
        for i in range(n):
            amp = 12000 * (1 - i / n)
            frames += struct.pack("<h", int(amp * math.sin(2 * math.pi * freq * i / rate)))
    with wave.open(str(target), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))


def apply_zap_frame(img_bytes: bytes, player_name: str, fault_num: int, shocked: bool) -> bytes:
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    w, h = img.size
    draw = ImageDraw.Draw(img)

    border_color = (255, 204, 0) if shocked else (56, 189, 248)
    border_w = max(8, int(min(w, h) * 0.02))
    draw.rectangle([0, 0, w - 1, h - 1], outline=border_color, width=border_w)

    banner_h = int(h * 0.14)
    draw.rectangle([0, h - banner_h, w, h], fill=(15, 23, 42))
    draw.line([(0, h - banner_h), (w, h - banner_h)], fill=border_color, width=4)

    try:
        font_lg = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", int(banner_h * 0.42))
        font_sm = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", int(banner_h * 0.25))
    except Exception:
        font_lg = font_sm = ImageFont.load_default()

    tag = "ZAPPED!" if shocked else "FAULT!"
    title_text = f"{tag} - {player_name.upper()}"
    sub_text = f"Touch #{fault_num}  |  Buzz Wire Challenge"

    draw.text((border_w + 16, h - banner_h + int(banner_h * 0.12)), title_text, fill=border_color, font=font_lg)
    draw.text((border_w + 18, h - banner_h + int(banner_h * 0.62)), sub_text, fill=(226, 232, 240), font=font_sm)

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=88)
    return out.getvalue()


def capture_and_upload_zap(session_id: str, player_name: str, fault_num: int, shocked: bool) -> None:
    if not settings.phone_snapshot_url or not s3_client:
        return
    try:
        time.sleep(0.15)  # Catch the human reaction
        with httpx.Client(timeout=3.5) as client:
            resp = client.get(settings.phone_snapshot_url)
            resp.raise_for_status()
            raw_jpeg = resp.content

        framed_jpeg = apply_zap_frame(raw_jpeg, player_name, fault_num, shocked)
        ts = int(time.time())
        key = f"sessions/{session_id}/zap_{ts}_{fault_num}.jpg"

        s3_client.put_object(
            Bucket=settings.s3_bucket,
            Key=key,
            Body=framed_jpeg,
            ContentType="image/jpeg",
        )
        url = s3_client.generate_presigned_url(
            "get_object", Params={"Bucket": settings.s3_bucket, "Key": key}, ExpiresIn=86400
        )
        broadcaster.broadcast("photo_uploaded", {
            "session_id": session_id,
            "player_name": player_name,
            "fault": fault_num,
            "url": url,
            "key": key,
        })
        log.info("Uploaded zap photo to Garage S3: s3://%s/%s", settings.s3_bucket, key)
    except Exception:
        log.exception("Failed to capture or upload zap photo for session %s", session_id)


# --------------------------------------------------------------------------- #
# Leaderboard (SQLite)
# --------------------------------------------------------------------------- #
class Leaderboard:
    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS entries (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       session_id TEXT,
                       player_name TEXT NOT NULL,
                       touch_count INTEGER NOT NULL,
                       completion_time_seconds REAL NOT NULL,
                       score INTEGER NOT NULL DEFAULT 0,
                       created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"""
            )
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(entries)")}
            if "score" not in cols:
                self._conn.execute("ALTER TABLE entries ADD COLUMN score INTEGER NOT NULL DEFAULT 0")
            if "session_id" not in cols:
                self._conn.execute("ALTER TABLE entries ADD COLUMN session_id TEXT")
            self._conn.commit()

    def add(self, session_id: str, name: str, faults: int, seconds: float, score: int) -> int:
        with self._lock:
            self._conn.execute(
                """INSERT INTO entries (session_id, player_name, touch_count, completion_time_seconds, score)
                   VALUES (?,?,?,?,?)""", (session_id, name, faults, seconds, score))
            self._conn.commit()
            better = self._conn.execute(
                """SELECT COUNT(*) FROM entries WHERE score > ?
                   OR (score = ? AND completion_time_seconds < ?)
                   OR (score = ? AND completion_time_seconds = ? AND touch_count < ?)""",
                (score, score, seconds, score, seconds, faults)).fetchone()[0]
        return better + 1

    def top(self, limit: int) -> List[LeaderboardEntry]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT player_name, touch_count, completion_time_seconds, score, session_id FROM entries
                   ORDER BY score DESC, completion_time_seconds ASC, touch_count ASC, id ASC
                   LIMIT ?""", (limit,)).fetchall()
        return [
            LeaderboardEntry(
                rank=i, player_name=r[0], touch_count=r[1],
                completion_time_seconds=r[2], score=r[3], session_id=r[4]
            )
            for i, r in enumerate(rows, start=1)
        ]

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM entries")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


_board: Optional[Leaderboard] = None


def init_board() -> Leaderboard:
    global _board
    _board = Leaderboard(settings.db_path)
    return _board


def get_board() -> Leaderboard:
    if _board is None:
        raise RuntimeError("leaderboard not initialised")
    return _board


def close_board() -> None:
    if _board is not None:
        _board.close()
