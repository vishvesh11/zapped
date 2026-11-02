"""Buzz-wire game backend (FastAPI).

Game flow (the Pico drives it; the control panel only starts/aborts):
  POST /session/start        panel: arm a game for a player       -> phase "waiting", timer 0
  POST /game/run-start       Pico: wand left the START hook       -> phase "running", timer runs
  POST /event/touch          Pico: wand touched the track (fault) -> chime, fault count, maybe shock
  POST /game/complete        Pico: wand hung on the END hook      -> score + leaderboard entry

Score = SCORE_BASE - SCORE_PER_SECOND * seconds - SCORE_PER_FAULT * faults   (minimum 0)

Run:  GAME_API_KEY=changeme python main.py        Docs: http://localhost:8000/docs
"""
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
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

# --------------------------------------------------------------------------- #
# Settings (override with environment variables)
# --------------------------------------------------------------------------- #
BASE_DIR = Path(__file__).resolve().parent
MEDIA_DIR = Path(os.getenv("MEDIA_DIR", BASE_DIR / "media")).resolve()
DB_PATH = os.getenv("DB_PATH", str(BASE_DIR / "leaderboard.db"))
API_KEY = os.getenv("GAME_API_KEY", "")  # empty = auth disabled (development only)
DEFAULT_MEDIA = "chime.wav"
ALLOWED_EXT = {".wav", ".ogg", ".mp3"}

# Shock safety limits. Keep in sync with firmware/config.py.
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


def compute_score(seconds: float, faults: int) -> int:
    return max(0, int(round(SCORE_BASE - SCORE_PER_SECOND * seconds - SCORE_PER_FAULT * faults)))


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
    phase: Optional[str] = None  # "waiting" (timer 0) or "running"
    touch_count: int = 0         # faults
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
    touch_count: int = Field(ge=0, le=10000)  # faults counted by the Pico
    completion_time_seconds: float = Field(gt=0, le=86400)


class LeaderboardEntry(BaseModel):
    rank: int
    player_name: str
    touch_count: int
    completion_time_seconds: float
    score: int


# --------------------------------------------------------------------------- #
# In-memory game state
# --------------------------------------------------------------------------- #
@dataclass
class Session:
    id: str
    player_name: str
    run_started_at: Optional[float] = None  # time.monotonic() when the wand left the start hook
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
        self.shock_enabled = False  # always starts disarmed
        self.shock_duration = SHOCK_DEFAULT_S
        self.media_file = DEFAULT_MEDIA
        self.session: Optional[Session] = None

    def config(self) -> ConfigResponse:
        return ConfigResponse(shock_enabled=self.shock_enabled,
                              shock_duration=self.shock_duration,
                              current_media_file=self.media_file)


state = GameState()


# --------------------------------------------------------------------------- #
# Media helpers
# --------------------------------------------------------------------------- #
def resolve_media(name: str) -> Path:
    """Return a safe path inside MEDIA_DIR or raise ValueError."""
    candidate = (MEDIA_DIR / name).resolve()
    if candidate.parent != MEDIA_DIR:
        raise ValueError("media file must be a plain filename inside the media folder")
    if candidate.suffix.lower() not in ALLOWED_EXT:
        raise ValueError("unsupported type; use " + ", ".join(sorted(ALLOWED_EXT)))
    if not candidate.is_file():
        raise ValueError("file not found: %s" % name)
    return candidate


def list_media() -> List[str]:
    if not MEDIA_DIR.exists():
        return []
    return sorted(p.name for p in MEDIA_DIR.iterdir()
                  if p.is_file() and p.suffix.lower() in ALLOWED_EXT)


def ensure_default_chime() -> None:
    """Synthesise a short two-tone chime if none exists, so it works out of the box."""
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
    log.info("Created default chime at %s", target)


class MediaPlayer:
    """Failure-tolerant wrapper around pygame.mixer (the game works without audio)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ready = False
        self._pg = None
        self._cache = {}

    def _init(self) -> bool:
        if self._ready:
            return True
        try:
            import pygame
            pygame.mixer.init()
            self._pg, self._ready = pygame, True
        except Exception as exc:  # no audio device, missing pygame, ...
            log.warning("Audio unavailable: %s", exc)
        return self._ready

    def play(self, path: Path) -> bool:
        with self._lock:
            if not self._init():
                return False
            try:
                sound = self._cache.get(path)
                if sound is None:
                    sound = self._pg.mixer.Sound(str(path))
                    self._cache[path] = sound
                sound.play()
                return True
            except Exception:
                log.exception("Failed to play %s", path)
                return False


player = MediaPlayer()


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
                       player_name TEXT NOT NULL,
                       touch_count INTEGER NOT NULL,
                       completion_time_seconds REAL NOT NULL,
                       score INTEGER NOT NULL DEFAULT 0,
                       created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"""
            )
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(entries)")}
            if "score" not in cols:  # database from an older version
                self._conn.execute("ALTER TABLE entries ADD COLUMN score INTEGER NOT NULL DEFAULT 0")
            self._conn.commit()

    def add(self, name: str, faults: int, seconds: float, score: int) -> int:
        """Insert an entry and return its rank (1 = best)."""
        with self._lock:
            self._conn.execute(
                """INSERT INTO entries (player_name, touch_count, completion_time_seconds, score)
                   VALUES (?,?,?,?)""", (name, faults, seconds, score))
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
                """SELECT player_name, touch_count, completion_time_seconds, score FROM entries
                   ORDER BY score DESC, completion_time_seconds ASC, touch_count ASC, id ASC
                   LIMIT ?""", (limit,)).fetchall()
        return [LeaderboardEntry(rank=i, player_name=r[0], touch_count=r[1],
                                 completion_time_seconds=r[2], score=r[3])
                for i, r in enumerate(rows, start=1)]

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM entries")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


board: Optional[Leaderboard] = None


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    global board
    ensure_default_chime()
    board = Leaderboard(DB_PATH)
    if not API_KEY:
        log.warning("GAME_API_KEY not set: API is unauthenticated (development only)")
    yield
    board.close()


app = FastAPI(title="Buzz Wire Game API", version="2.0.0", lifespan=lifespan)


def require_api_key(x_api_key: str = Header(default="")) -> None:
    if API_KEY and not secrets.compare_digest(x_api_key, API_KEY):
        raise HTTPException(status_code=401, detail="invalid or missing API key")


router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)])


@app.get("/health")
async def health():
    return {"status": "ok"}


# ---- config ---------------------------------------------------------------- #
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
        log.info("Config now: %s", state.config().model_dump())
        return state.config()


@router.get("/media", response_model=MediaListResponse)
async def get_media():
    return MediaListResponse(files=list_media())


# ---- session --------------------------------------------------------------- #
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
        live_score=compute_score(elapsed, s.touch_count) if s.running else int(SCORE_BASE))


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
        log.info("Game armed for %s (waiting for wand to leave the start hook)", body.player_name)
        return _session_response()


@router.delete("/session", response_model=SessionResponse)
async def abort_session():
    with state.lock:
        state.session = None
        log.info("Session aborted")
        return _session_response()


# ---- game events ------------------------------------------------------------ #
@router.post("/game/run-start", response_model=SessionResponse)
async def run_start(body: RunStart):
    """Wand left the start hook: start the timer. Safe to call twice (Pico retries)."""
    with state.lock:
        s = state.session
        if s is None or s.id != body.session_id:
            raise HTTPException(status_code=409, detail="no matching armed session")
        if not s.running:
            s.run_started_at = time.monotonic()
            s.touch_count = 0
            s.shocks_fired = 0
            s.last_shock_at = None
            log.info("%s: run started", s.player_name)
        return _session_response()


@router.post("/event/touch", response_model=TouchResponse)
async def touch_event(background: BackgroundTasks, payload: Optional[TouchEvent] = None):
    with state.lock:
        sess = state.session
        # Ignore events that belong to a stale session.
        if sess and payload and payload.session_id and payload.session_id != sess.id:
            sess = None
        running = sess is not None and sess.running
        shock, duration, faults = False, state.shock_duration, 0
        if running:
            sess.touch_count += 1
            if payload and payload.fault_count and payload.fault_count > sess.touch_count:
                sess.touch_count = payload.fault_count  # the Pico is the source of truth
            now = time.monotonic()
            cooled = sess.last_shock_at is None or now - sess.last_shock_at >= SHOCK_COOLDOWN_S
            if state.shock_enabled and cooled and sess.shocks_fired < MAX_SHOCKS_PER_SESSION:
                shock = True
                sess.shocks_fired += 1
                sess.last_shock_at = now
            faults = sess.touch_count
        media = state.media_file

    queued = True
    try:
        background.add_task(player.play, resolve_media(media))
    except ValueError as exc:
        queued = False
        log.warning("Chime not played: %s", exc)

    return TouchResponse(chime_queued=queued, running=running, touch_count=faults,
                         shock=shock, shock_duration=duration)


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
                log.warning("Device time %.1fs vs server %.1fs; using server time",
                            seconds, server_elapsed)
                seconds = server_elapsed
        faults = max(sess.touch_count, body.touch_count)
        name = sess.player_name
        state.session = None
    seconds = round(seconds, 2)
    score = compute_score(seconds, faults)
    rank = board.add(name, faults, seconds, score)
    log.info("%s: %.2fs, %d faults, score %d (rank %d)", name, seconds, faults, score, rank)
    return LeaderboardEntry(rank=rank, player_name=name, touch_count=faults,
                            completion_time_seconds=seconds, score=score)


@router.get("/leaderboard", response_model=List[LeaderboardEntry])
async def get_leaderboard(limit: int = 20):
    return board.top(max(1, min(limit, 200)))


@router.delete("/leaderboard")
async def clear_leaderboard():
    board.clear()
    return {"cleared": True}


app.include_router(router)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
