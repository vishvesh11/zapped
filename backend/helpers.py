"""Shared infrastructure for the Buzz-wire backend.

Holds the in-memory game state, the SSE broadcaster, media/camera helpers and
the SQLite leaderboard. Route handlers in ``routes.py`` build on these.
"""
import asyncio
import contextlib
import io
import json
import logging
import math
import os
import re
import sqlite3
import statistics
import struct
import tempfile
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import boto3
from botocore.client import Config as BotoConfig
import httpx
from PIL import Image, ImageDraw, ImageFont

from config import settings
from schemas import ConfigResponse, LeaderboardEntry, SessionResponse, TeamEntry

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
    """Fan-out of SSE payloads to every connected client.

    ``broadcast`` may be called from the event loop *or* from worker threads (e.g.
    the photo-upload background task). ``asyncio.Queue`` is not thread-safe, so
    cross-thread puts are marshalled onto the subscriber's loop.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: List[Tuple[asyncio.Queue, asyncio.AbstractEventLoop]] = []

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=50)
        loop = asyncio.get_running_loop()
        with self._lock:
            self._subscribers.append((q, loop))
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers = [(sq, lp) for sq, lp in self._subscribers if sq is not q]

    @staticmethod
    def _put(q: asyncio.Queue, payload: str) -> None:
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            pass  # slow client: drop rather than block everyone else

    def broadcast(self, event_type: str, data: dict) -> None:
        payload = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        with self._lock:
            subscribers = list(self._subscribers)
        for q, loop in subscribers:
            if loop is current:
                self._put(q, payload)
            elif not loop.is_closed():
                loop.call_soon_threadsafe(self._put, q, payload)


broadcaster = EventBroadcaster()


# --------------------------------------------------------------------------- #
# In-memory game state
# --------------------------------------------------------------------------- #
@dataclass
class Session:
    id: str
    player_name: str
    team: Optional[str] = None
    run_started_at: Optional[float] = None
    touch_count: int = 0

    @property
    def running(self) -> bool:
        return self.run_started_at is not None

    def elapsed(self) -> float:
        return time.monotonic() - self.run_started_at if self.running else 0.0


class GameState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.session: Optional[Session] = None
        self.reset_sounds()

    def reset_sounds(self) -> None:
        self.media_file = settings.default_media
        self.penalty_media_file: Optional[str] = None
        self.penalty_threshold = settings.penalty_threshold

    def config(self) -> ConfigResponse:
        return ConfigResponse(
            current_media_file=self.media_file,
            penalty_media_file=self.penalty_media_file,
            penalty_threshold=self.penalty_threshold,
        )

    def sound_for(self, touch_count: int) -> Tuple[str, bool]:
        """(file to play, is_penalty) for the touch that brought the count to ``touch_count``."""
        if self.penalty_media_file and touch_count > self.penalty_threshold:
            return self.penalty_media_file, True
        return self.media_file, False

    # ---- persistence (sound choices survive pod restarts) ---- #
    def save_sounds(self) -> None:
        path = settings.game_settings_path
        try:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "current_media_file": self.media_file,
                "penalty_media_file": self.penalty_media_file,
                "penalty_threshold": self.penalty_threshold,
            }))
            os.replace(tmp, path)
        except OSError:
            log.exception("could not save game settings to %s", path)

    def load_sounds(self) -> None:
        self.reset_sounds()
        path = settings.game_settings_path
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            log.exception("ignoring unreadable game settings at %s", path)
            return

        def usable(name):
            try:
                return bool(name) and resolve_media(name) is not None
            except ValueError:
                log.warning("saved sound %r is no longer available; ignoring it", name)
                return False

        if usable(data.get("current_media_file")):
            self.media_file = data["current_media_file"]
        if usable(data.get("penalty_media_file")):
            self.penalty_media_file = data["penalty_media_file"]
        threshold = data.get("penalty_threshold")
        if isinstance(threshold, int) and 0 <= threshold <= 100:
            self.penalty_threshold = threshold


state = GameState()


def session_response() -> SessionResponse:
    s = state.session
    if s is None:
        return SessionResponse(active=False)
    elapsed = s.elapsed()
    return SessionResponse(
        active=True, session_id=s.id, player_name=s.player_name, team=s.team,
        phase="running" if s.running else "waiting",
        touch_count=s.touch_count,
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


_MEDIA_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,95}$")


def _looks_like(ext: str, data: bytes) -> bool:
    """Cheap magic-byte check so only real audio of the declared type is stored."""
    if ext == ".wav":
        return data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    if ext == ".ogg":
        return data[:4] == b"OggS"
    if ext == ".mp3":
        return data[:3] == b"ID3" or (len(data) > 1 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0)
    return False


def save_media(name: str, data: bytes) -> Path:
    """Validate and atomically store an uploaded sound in the media folder.

    Raises ``ValueError`` with a user-facing message if the name or content is rejected.
    """
    if not _MEDIA_NAME_RE.fullmatch(name) or ".." in name:
        raise ValueError("file name may only contain letters, digits, spaces, '.', '_' and '-'")
    ext = Path(name).suffix.lower()
    if ext not in settings.allowed_ext:
        raise ValueError("unsupported type: use " + ", ".join(sorted(settings.allowed_ext)))
    if not data:
        raise ValueError("file is empty")
    if len(data) > settings.max_media_bytes:
        raise ValueError(f"file is larger than {settings.max_media_bytes // (1024 * 1024)} MB")
    if not _looks_like(ext, data):
        raise ValueError(f"file content is not valid {ext[1:].upper()} audio")

    media_dir = settings.media_dir_resolved
    media_dir.mkdir(parents=True, exist_ok=True)
    target = media_dir / name
    fd, tmp = tempfile.mkstemp(dir=media_dir, prefix=".upload-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return target


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


def apply_touch_frame(img_bytes: bytes, player_name: str, fault_num: int) -> bytes:
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    w, h = img.size
    draw = ImageDraw.Draw(img)

    border_color = (56, 189, 248)
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

    title_text = f"TOUCH! - {player_name.upper()}"
    sub_text = f"Touch #{fault_num}  |  Buzz Wire Challenge"

    draw.text((border_w + 16, h - banner_h + int(banner_h * 0.12)), title_text, fill=border_color, font=font_lg)
    draw.text((border_w + 18, h - banner_h + int(banner_h * 0.62)), sub_text, fill=(226, 232, 240), font=font_sm)

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=88)
    return out.getvalue()


def capture_and_upload_touch(session_id: str, player_name: str, fault_num: int) -> Optional[dict]:
    """Grab a frame from the camera, frame it, upload to Garage S3 and announce it.

    Returns the uploaded photo info (key/url) on success, or ``None`` if the camera
    or S3 is not configured / reachable.
    """
    if not settings.phone_snapshot_url or not s3_client:
        log.warning("capture skipped: camera URL or S3 client not configured")
        return None
    try:
        time.sleep(0.15)  # Catch the human reaction
        with httpx.Client(timeout=3.5) as client:
            resp = client.get(settings.phone_snapshot_url)
            resp.raise_for_status()
            raw_jpeg = resp.content

        framed_jpeg = apply_touch_frame(raw_jpeg, player_name, fault_num)
        ts = int(time.time())
        key = f"sessions/{session_id}/touch_{ts}_{fault_num}.jpg"

        s3_client.put_object(
            Bucket=settings.s3_bucket,
            Key=key,
            Body=framed_jpeg,
            ContentType="image/jpeg",
        )
        url = s3_client.generate_presigned_url(
            "get_object", Params={"Bucket": settings.s3_bucket, "Key": key}, ExpiresIn=86400
        )
        info = {
            "session_id": session_id,
            "player_name": player_name,
            "fault": fault_num,
            "url": url,
            "key": key,
        }
        broadcaster.broadcast("photo_uploaded", info)
        log.info("Uploaded touch photo to Garage S3: s3://%s/%s", settings.s3_bucket, key)
        return info
    except Exception:
        log.exception("Failed to capture or upload touch photo for session %s", session_id)
        return None


def camera_selftest(use_camera: bool = True) -> dict:
    """Diagnostic: capture (or synthesize) a frame and upload it to Garage S3 under a
    dummy session so you can confirm the camera + bucket wiring end-to-end.

    If ``use_camera`` is True it pulls a real frame from ``PHONE_SNAPSHOT_URL``; on any
    camera failure (or when False) it falls back to a generated placeholder image so the
    S3 upload path can still be verified independently.
    """
    result = {
        "camera_url": settings.phone_snapshot_url,
        "s3_endpoint": settings.s3_endpoint,
        "s3_bucket": settings.s3_bucket,
        "camera_ok": False,
        "s3_configured": s3_client is not None,
        "uploaded": False,
        "source": None,
        "key": None,
        "url": None,
        "error": None,
    }

    raw_jpeg = None
    if use_camera and settings.phone_snapshot_url:
        try:
            with httpx.Client(timeout=3.5) as client:
                resp = client.get(settings.phone_snapshot_url)
                resp.raise_for_status()
                raw_jpeg = resp.content
            result["camera_ok"] = True
            result["source"] = "camera"
        except Exception as exc:
            result["error"] = f"camera fetch failed: {exc!r}"

    if raw_jpeg is None:
        # Synthesize a dummy frame so the S3 path is still exercised.
        img = Image.new("RGB", (640, 480), (15, 23, 42))
        draw = ImageDraw.Draw(img)
        draw.text((20, 20), "CAMERA SELF-TEST (dummy frame)", fill=(56, 189, 248))
        draw.text((20, 50), time.strftime("%Y-%m-%d %H:%M:%S"), fill=(226, 232, 240))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        raw_jpeg = buf.getvalue()
        if result["source"] is None:
            result["source"] = "dummy"

    if s3_client is None:
        result["error"] = result["error"] or "S3 client not configured (missing S3_ACCESS_KEY/S3_SECRET_KEY)"
        return result

    try:
        framed = apply_touch_frame(raw_jpeg, "SELFTEST", 0)
        ts = int(time.time())
        # Kept outside "sessions/" so diagnostics never show up in the public gallery.
        key = f"selftest/selftest_{ts}.jpg"
        s3_client.put_object(
            Bucket=settings.s3_bucket, Key=key, Body=framed, ContentType="image/jpeg"
        )
        url = s3_client.generate_presigned_url(
            "get_object", Params={"Bucket": settings.s3_bucket, "Key": key}, ExpiresIn=86400
        )
        result["uploaded"] = True
        result["key"] = key
        result["url"] = url
        log.info("Camera self-test uploaded to s3://%s/%s", settings.s3_bucket, key)
    except Exception as exc:
        result["error"] = f"S3 upload failed: {exc!r}"
        log.exception("Camera self-test S3 upload failed")

    return result


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
            if "team" not in cols:
                self._conn.execute("ALTER TABLE entries ADD COLUMN team TEXT")
            self._conn.commit()

    def add(self, session_id: str, name: str, faults: int, seconds: float, score: int,
            team: Optional[str] = None) -> int:
        with self._lock:
            self._conn.execute(
                """INSERT INTO entries (session_id, player_name, touch_count, completion_time_seconds, score, team)
                   VALUES (?,?,?,?,?,?)""", (session_id, name, faults, seconds, score, team))
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
                """SELECT player_name, touch_count, completion_time_seconds, score, session_id, team
                   FROM entries
                   ORDER BY score DESC, completion_time_seconds ASC, touch_count ASC, id ASC
                   LIMIT ?""", (limit,)).fetchall()
        return [
            LeaderboardEntry(
                rank=i, player_name=r[0], touch_count=r[1],
                completion_time_seconds=r[2], score=r[3], session_id=r[4], team=r[5]
            )
            for i, r in enumerate(rows, start=1)
        ]

    def teams(self) -> List[TeamEntry]:
        """Team standings ranked by the median of each player's best score.

        Players are matched case-insensitively within a team; runs without a team are
        left out. Ties go to the team with more players, then the higher best score.
        """
        with self._lock:
            rows = self._conn.execute(
                """SELECT team, player_name, score, completion_time_seconds, touch_count
                   FROM entries WHERE team IS NOT NULL AND TRIM(team) != ''
                   ORDER BY id ASC""").fetchall()

        groups = {}  # team key -> {"name": display name, "runs": n, "best": {player key: row}}
        for team, player, score, seconds, touches in rows:
            g = groups.setdefault(team.casefold(), {"name": team, "runs": 0, "best": {}})
            g["runs"] += 1
            run = (score, -seconds, -touches, player, seconds, touches)
            key = player.strip().casefold()
            if key not in g["best"] or run[:3] > g["best"][key][:3]:
                g["best"][key] = run

        standings = []
        for g in groups.values():
            best = list(g["best"].values())
            top = max(best, key=lambda r: r[:3])
            standings.append({
                "team": g["name"],
                "median_score": round(statistics.median(r[0] for r in best), 1),
                "players": len(best),
                "runs": g["runs"],
                "best_player": top[3],
                "best_score": top[0],
                "median_time_seconds": round(statistics.median(r[4] for r in best), 2),
                "median_touches": statistics.median(r[5] for r in best),
            })
        standings.sort(key=lambda t: (-t["median_score"], -t["players"], -t["best_score"],
                                      t["team"].casefold()))
        return [TeamEntry(rank=i, **t) for i, t in enumerate(standings, start=1)]

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
