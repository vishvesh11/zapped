"""Buzz-wire game backend (FastAPI) - Headscale Camera + Garage S3 + Dual Web UI."""
import asyncio
import io
import json
import logging
import math
import os
import secrets
import sqlite3
import struct
import threading
import time
import uuid
import wave
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncGenerator, List, Optional

import boto3
from botocore.client import Config as BotoConfig
import httpx
from PIL import Image, ImageDraw, ImageFont
from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator

# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
BASE_DIR = Path(__file__).resolve().parent
MEDIA_DIR = Path(os.getenv("MEDIA_DIR", BASE_DIR / "media")).resolve()
TEMPLATES_DIR = Path(os.getenv("TEMPLATES_DIR", BASE_DIR / "templates")).resolve()
DB_PATH = os.getenv("DB_PATH", str(BASE_DIR / "leaderboard.db"))
API_KEY = os.getenv("GAME_API_KEY", "")
DEFAULT_MEDIA = "chime.wav"
ALLOWED_EXT = {".wav", ".ogg", ".mp3"}

# Headscale Camera IP & Garage S3 settings
PHONE_SNAPSHOT_URL = os.getenv("PHONE_SNAPSHOT_URL", "http://100.64.0.15:8080/shot.jpg")
S3_ENDPOINT = os.getenv("S3_ENDPOINT", "https://s3.vishvesh.me")
S3_REGION = os.getenv("S3_REGION", "garage")
S3_BUCKET = os.getenv("S3_BUCKET", "buzzwire-zaps")
S3_ACCESS_KEY = os.getenv("S3_ACCESS_KEY", "")
S3_SECRET_KEY = os.getenv("S3_SECRET_KEY", "")

# Shock safety limits
SHOCK_MIN_S = 0.05
SHOCK_MAX_S = float(os.getenv("SHOCK_MAX_S", "0.5"))
SHOCK_DEFAULT_S = 0.15
SHOCK_COOLDOWN_S = float(os.getenv("SHOCK_COOLDOWN_S", "3.0"))
MAX_SHOCKS_PER_SESSION = int(os.getenv("MAX_SHOCKS_PER_SESSION", "3"))

# Score formula
SCORE_BASE = float(os.getenv("SCORE_BASE", "1000"))
SCORE_PER_SECOND = float(os.getenv("SCORE_PER_SECOND", "5"))
SCORE_PER_FAULT = float(os.getenv("SCORE_PER_FAULT", "50"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("buzzwire")

s3_client = None
if S3_ACCESS_KEY and S3_SECRET_KEY:
    s3_client = boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        region_name=S3_REGION,
        aws_access_key_id=S3_ACCESS_KEY,
        aws_secret_access_key=S3_SECRET_KEY,
        config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def compute_score(seconds: float, faults: int) -> int:
    return max(0, int(round(SCORE_BASE - SCORE_PER_SECOND * seconds - SCORE_PER_FAULT * faults)))


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
# Schemas
# --------------------------------------------------------------------------- #
class ConfigResponse(BaseModel):
    shock_enabled: bool
    shock_duration: float
    current_media_file: str
    shock_duration_min: float = SHOCK_MIN_S
    shock_duration_max: float = SHOCK_MAX_S
    score_base: float = SCORE_BASE
    score_per_second: float = SCORE_PER_SECOND
    score_per_fault: float = SCORE_PER_FAULT


class ConfigUpdate(BaseModel):
    shock_enabled: Optional[bool] = None
    shock_duration: Optional[float] = Field(None, ge=SHOCK_MIN_S, le=SHOCK_MAX_S)
    current_media_file: Optional[str] = Field(None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def at_least_one(self):
        if (self.shock_enabled is None and self.shock_duration is None
                and self.current_media_file is None):
            raise ValueError("provide at least one field to update")
        return self


class MediaListResponse(BaseModel):
    files: List[str]


class SessionStart(BaseModel):
    player_name: str = Field(min_length=1, max_length=24)

    @field_validator("player_name")
    @classmethod
    def clean_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("player_name must not be blank")
        return v


class SessionResponse(BaseModel):
    active: bool
    session_id: Optional[str] = None
    player_name: Optional[str] = None
    phase: Optional[str] = None
    touch_count: int = 0
    shocks_fired: int = 0
    elapsed_seconds: float = 0.0
    live_score: int = 0


class RunStart(BaseModel):
    session_id: str = Field(max_length=64)
    device_id: Optional[str] = Field(None, max_length=64)


class TouchEvent(BaseModel):
    device_id: Optional[str] = Field(None, max_length=64)
    session_id: Optional[str] = Field(None, max_length=64)
    fault_count: Optional[int] = Field(None, ge=0, le=10000)
    uptime_ms: Optional[int] = Field(None, ge=0)


class TouchResponse(BaseModel):
    chime_queued: bool
    running: bool
    touch_count: int
    shock: bool
    shock_duration: float


class CompleteRequest(BaseModel):
    session_id: str = Field(max_length=64)
    touch_count: int = Field(ge=0, le=10000)
    completion_time_seconds: float = Field(gt=0, le=86400)


class LeaderboardEntry(BaseModel):
    rank: int
    player_name: str
    touch_count: int
    completion_time_seconds: float
    score: int
    session_id: Optional[str] = None


class PhotoItem(BaseModel):
    key: str
    url: str
    session_id: str
    timestamp: str


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
        self.shock_duration = SHOCK_DEFAULT_S
        self.media_file = DEFAULT_MEDIA
        self.session: Optional[Session] = None

    def config(self) -> ConfigResponse:
        return ConfigResponse(
            shock_enabled=self.shock_enabled,
            shock_duration=self.shock_duration,
            current_media_file=self.media_file,
        )


state = GameState()


# --------------------------------------------------------------------------- #
# Media & Camera Helpers
# --------------------------------------------------------------------------- #
def resolve_media(name: str) -> Path:
    candidate = (MEDIA_DIR / name).resolve()
    if candidate.parent != MEDIA_DIR:
        raise ValueError("media file must be inside media folder")
    if candidate.suffix.lower() not in ALLOWED_EXT:
        raise ValueError("unsupported type")
    if not candidate.is_file():
        raise ValueError(f"file not found: {name}")
    return candidate


def list_media() -> List[str]:
    if not MEDIA_DIR.exists():
        return []
    return sorted(p.name for p in MEDIA_DIR.iterdir()
                  if p.is_file() and p.suffix.lower() in ALLOWED_EXT)


def ensure_default_chime() -> None:
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    target = MEDIA_DIR / DEFAULT_MEDIA
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
    if not PHONE_SNAPSHOT_URL or not s3_client:
        return
    try:
        time.sleep(0.15)  # Catch the human reaction
        with httpx.Client(timeout=3.5) as client:
            resp = client.get(PHONE_SNAPSHOT_URL)
            resp.raise_for_status()
            raw_jpeg = resp.content

        framed_jpeg = apply_zap_frame(raw_jpeg, player_name, fault_num, shocked)
        ts = int(time.time())
        key = f"sessions/{session_id}/zap_{ts}_{fault_num}.jpg"

        s3_client.put_object(
            Bucket=S3_BUCKET,
            Key=key,
            Body=framed_jpeg,
            ContentType="image/jpeg",
        )
        url = s3_client.generate_presigned_url(
            "get_object", Params={"Bucket": S3_BUCKET, "Key": key}, ExpiresIn=86400
        )
        broadcaster.broadcast("photo_uploaded", {
            "session_id": session_id,
            "player_name": player_name,
            "fault": fault_num,
            "url": url,
            "key": key,
        })
        log.info("Uploaded zap photo to Garage S3: s3://%s/%s", S3_BUCKET, key)
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


board: Optional[Leaderboard] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global board
    ensure_default_chime()
    board = Leaderboard(DB_PATH)
    yield
    board.close()


app = FastAPI(title="Buzz Wire Game API", version="2.2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def require_api_key(x_api_key: str = Header(default="")) -> None:
    if API_KEY and not secrets.compare_digest(x_api_key, API_KEY):
        raise HTTPException(status_code=401, detail="invalid or missing API key")


router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)])
public_router = APIRouter(prefix="/public")


# ---- HTML Frontend Routes ------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
async def serve_public_ui():
    return (TEMPLATES_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/admin", response_class=HTMLResponse)
async def serve_admin_ui():
    return (TEMPLATES_DIR / "admin.html").read_text(encoding="utf-8")


@app.get("/health")
async def health():
    return {"status": "ok"}


# ---- Public Data & Garage S3 Gallery Routes ------------------------------- #
@public_router.get("/leaderboard", response_model=List[LeaderboardEntry])
async def public_leaderboard(limit: int = 50):
    return board.top(max(1, min(limit, 200)))


@public_router.get("/session", response_model=SessionResponse)
async def public_session():
    with state.lock:
        return _session_response()


@public_router.get("/photos", response_model=List[PhotoItem])
async def list_photos(session_id: Optional[str] = None, limit: int = 24):
    """Fetches zap photos from Garage S3 (either for a specific player session or latest overall)."""
    if not s3_client:
        return []
    prefix = f"sessions/{session_id}/" if session_id else "sessions/"
    try:
        res = s3_client.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix)
        objects = sorted(res.get("Contents", []), key=lambda x: x["LastModified"], reverse=True)[:limit]
        items = []
        for obj in objects:
            key = obj["Key"]
            parts = key.split("/")
            sid = parts[1] if len(parts) > 2 else "unknown"
            url = s3_client.generate_presigned_url(
                "get_object", Params={"Bucket": S3_BUCKET, "Key": key}, ExpiresIn=86400
            )
            items.append(PhotoItem(
                key=key,
                url=url,
                session_id=sid,
                timestamp=obj["LastModified"].isoformat()
            ))
        return items
    except Exception:
        log.exception("Failed listing photos from Garage S3")
        return []


@public_router.get("/media/{filename}")
async def serve_media_file(filename: str):
    try:
        return FileResponse(resolve_media(filename))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@public_router.get("/events/stream")
async def events_stream(request: Request) -> StreamingResponse:
    async def event_generator() -> AsyncGenerator[str, None]:
        q = broadcaster.subscribe()
        try:
            yield f"event: connected\ndata: {json.dumps({'status': 'listening'})}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield msg
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            broadcaster.unsubscribe(q)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ---- Admin & Pico Protected Routes ---------------------------------------- #
@router.get("/config", response_model=ConfigResponse)
async def get_config():
    with state.lock:
        return state.config()


@router.post("/config", response_model=ConfigResponse)
async def update_config(update: ConfigUpdate):
    if update.current_media_file is not None:
        try:
            resolve_media(update.current_media_file)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
    with state.lock:
        if update.shock_enabled is not None:
            state.shock_enabled = update.shock_enabled
        if update.shock_duration is not None:
            state.shock_duration = update.shock_duration
        if update.current_media_file is not None:
            state.media_file = update.current_media_file
        cfg = state.config()
    broadcaster.broadcast("config_update", cfg.model_dump())
    return cfg


@router.get("/media", response_model=MediaListResponse)
async def get_media():
    return MediaListResponse(files=list_media())


def _session_response() -> SessionResponse:
    s = state.session
    if s is None:
        return SessionResponse(active=False)
    elapsed = s.elapsed()
    return SessionResponse(
        active=True, session_id=s.id, player_name=s.player_name,
        phase="running" if s.running else "waiting",
        touch_count=s.touch_count, shocks_fired=s.shocks_fired,
        elapsed_seconds=round(elapsed, 2),
        live_score=compute_score(elapsed, s.touch_count) if s.running else int(SCORE_BASE),
    )


@router.get("/session", response_model=SessionResponse)
async def get_session():
    with state.lock:
        return _session_response()


@router.post("/session/start", response_model=SessionResponse)
async def start_session(body: SessionStart):
    with state.lock:
        if state.session is not None:
            raise HTTPException(status_code=409, detail="a game is already in progress")
        state.session = Session(id=uuid.uuid4().hex, player_name=body.player_name)
        resp = _session_response()
    broadcaster.broadcast("session_update", resp.model_dump())
    return resp


@router.delete("/session", response_model=SessionResponse)
async def abort_session():
    with state.lock:
        state.session = None
        resp = _session_response()
    broadcaster.broadcast("session_update", resp.model_dump())
    return resp


@router.post("/game/run-start", response_model=SessionResponse)
async def run_start(body: RunStart):
    with state.lock:
        s = state.session
        if s is None or s.id != body.session_id:
            raise HTTPException(status_code=409, detail="no matching armed session")
        if not s.running:
            s.run_started_at = time.monotonic()
            s.touch_count = 0
            s.shocks_fired = 0
            s.last_shock_at = None
        resp = _session_response()
    broadcaster.broadcast("session_update", resp.model_dump())
    return resp


@router.post("/event/touch", response_model=TouchResponse)
async def touch_event(background: BackgroundTasks, payload: Optional[TouchEvent] = None):
    with state.lock:
        sess = state.session
        if sess and payload and payload.session_id and payload.session_id != sess.id:
            sess = None
        running = sess is not None and sess.running
        shock, duration, faults = False, state.shock_duration, 0
        sess_id, player_name = None, ""
        if running:
            sess.touch_count += 1
            if payload and payload.fault_count and payload.fault_count > sess.touch_count:
                sess.touch_count = payload.fault_count
            now = time.monotonic()
            cooled = sess.last_shock_at is None or now - sess.last_shock_at >= SHOCK_COOLDOWN_S
            if state.shock_enabled and cooled and sess.shocks_fired < MAX_SHOCKS_PER_SESSION:
                shock = True
                sess.shocks_fired += 1
                sess.last_shock_at = now
            faults = sess.touch_count
            sess_id = sess.id
            player_name = sess.player_name
        media = state.media_file
        resp_state = _session_response()

    broadcaster.broadcast("play_chime", {
        "audio_url": f"/public/media/{media}",
        "touch_count": faults,
        "shock": shock,
        "session_id": sess_id,
    })
    broadcaster.broadcast("session_update", resp_state.model_dump())

    # Snap photo when a player gets shocked (change `if running and shock:` to `if running:` to snap all faults)
    if running and sess_id and shock:
        background.add_task(capture_and_upload_zap, sess_id, player_name, faults, shock)

    return TouchResponse(
        chime_queued=True, running=running, touch_count=faults,
        shock=shock, shock_duration=duration
    )


@router.post("/game/complete", response_model=LeaderboardEntry)
async def complete_game(body: CompleteRequest):
    with state.lock:
        sess = state.session
        if sess is None or sess.id != body.session_id:
            raise HTTPException(status_code=409, detail="no matching active session")
        seconds = body.completion_time_seconds
        if sess.running:
            server_elapsed = sess.elapsed()
            if abs(seconds - server_elapsed) > 10:
                seconds = server_elapsed
        faults = max(sess.touch_count, body.touch_count)
        name = sess.player_name
        sess_id = sess.id
        state.session = None

    seconds = round(seconds, 2)
    score = compute_score(seconds, faults)
    rank = board.add(sess_id, name, faults, seconds, score)
    entry = LeaderboardEntry(
        rank=rank, player_name=name, touch_count=faults,
        completion_time_seconds=seconds, score=score, session_id=sess_id
    )
    broadcaster.broadcast("game_complete", entry.model_dump())
    broadcaster.broadcast("session_update", {"active": False})
    return entry


@router.get("/leaderboard", response_model=List[LeaderboardEntry])
async def get_leaderboard(limit: int = 20):
    return board.top(max(1, min(limit, 200)))


@router.delete("/leaderboard")
async def clear_leaderboard():
    board.clear()
    broadcaster.broadcast("leaderboard_cleared", {})
    return {"cleared": True}


app.include_router(router)
app.include_router(public_router)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))