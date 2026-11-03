"""API routes for the Buzz-wire game backend.

Two routers are exported:
  * ``router``        - protected endpoints (admin + Pico), guarded by an API key.
  * ``public_router`` - unauthenticated data + SSE stream for the web UIs.
"""
import asyncio
import json
import secrets
import time
import uuid
from typing import AsyncGenerator, List, Optional

from fastapi import (
    APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request,
)
from fastapi.responses import FileResponse, StreamingResponse

from config import settings
from helpers import (
    Session,
    broadcaster,
    camera_selftest,
    capture_and_upload_touch,
    compute_score,
    get_board,
    list_media,
    log,
    resolve_media,
    s3_client,
    session_response,
    state,
)
from schemas import (
    CompleteRequest,
    ConfigResponse,
    ConfigUpdate,
    LeaderboardEntry,
    MediaListResponse,
    PhotoItem,
    RunStart,
    SessionResponse,
    SessionStart,
    TouchEvent,
    TouchResponse,
)


def require_api_key(x_api_key: str = Header(default="")) -> None:
    if settings.api_key and not secrets.compare_digest(x_api_key, settings.api_key):
        raise HTTPException(status_code=401, detail="invalid or missing API key")


router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)])
public_router = APIRouter(prefix="/public")


# ---- Public Data & Garage S3 Gallery Routes ------------------------------- #
@public_router.get("/leaderboard", response_model=List[LeaderboardEntry])
async def public_leaderboard(limit: int = 50):
    return get_board().top(max(1, min(limit, 200)))


@public_router.get("/session", response_model=SessionResponse)
async def public_session():
    with state.lock:
        return session_response()


@public_router.get("/photos", response_model=List[PhotoItem])
async def list_photos(session_id: Optional[str] = None, limit: int = 24):
    """Fetches zap photos from Garage S3 (either for a specific player session or latest overall)."""
    if not s3_client:
        return []
    prefix = f"sessions/{session_id}/" if session_id else "sessions/"
    try:
        res = s3_client.list_objects_v2(Bucket=settings.s3_bucket, Prefix=prefix)
        objects = sorted(res.get("Contents", []), key=lambda x: x["LastModified"], reverse=True)[:limit]
        items = []
        for obj in objects:
            key = obj["Key"]
            parts = key.split("/")
            sid = parts[1] if len(parts) > 2 else "unknown"
            url = s3_client.generate_presigned_url(
                "get_object", Params={"Bucket": settings.s3_bucket, "Key": key}, ExpiresIn=86400
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
        if update.current_media_file is not None:
            state.media_file = update.current_media_file
        cfg = state.config()
    broadcaster.broadcast("config_update", cfg.model_dump())
    return cfg


@router.get("/media", response_model=MediaListResponse)
async def get_media():
    return MediaListResponse(files=list_media())


@router.post("/camera/test")
async def camera_test(use_camera: bool = True):
    """Trigger a camera capture + Garage S3 upload with a dummy session/name so you can
    verify the pipeline. Pass ``use_camera=false`` to upload a synthetic frame and test
    S3 alone. Returns a diagnostic report (camera_ok, uploaded, key, url, error)."""
    return camera_selftest(use_camera=use_camera)


@router.get("/session", response_model=SessionResponse)
async def get_session():
    with state.lock:
        return session_response()


@router.post("/session/start", response_model=SessionResponse)
async def start_session(body: SessionStart):
    with state.lock:
        if state.session is not None:
            raise HTTPException(status_code=409, detail="a game is already in progress")
        state.session = Session(id=uuid.uuid4().hex, player_name=body.player_name)
        resp = session_response()
    broadcaster.broadcast("session_update", resp.model_dump())
    return resp


@router.delete("/session", response_model=SessionResponse)
async def abort_session():
    with state.lock:
        state.session = None
        resp = session_response()
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
        resp = session_response()
    broadcaster.broadcast("session_update", resp.model_dump())
    return resp


@router.post("/event/touch", response_model=TouchResponse)
async def touch_event(background: BackgroundTasks, payload: Optional[TouchEvent] = None):
    with state.lock:
        sess = state.session
        if sess and payload and payload.session_id and payload.session_id != sess.id:
            sess = None
        running = sess is not None and sess.running
        faults = 0
        sess_id, player_name = None, ""
        if running:
            sess.touch_count += 1
            if payload and payload.fault_count and payload.fault_count > sess.touch_count:
                sess.touch_count = payload.fault_count
            faults = sess.touch_count
            sess_id = sess.id
            player_name = sess.player_name
        media = state.media_file
        resp_state = session_response()

    broadcaster.broadcast("play_chime", {
        "audio_url": f"/public/media/{media}",
        "touch_count": faults,
        "session_id": sess_id,
    })
    broadcaster.broadcast("session_update", resp_state.model_dump())

    # Snap a photo on every wand touch.
    if running and sess_id:
        background.add_task(capture_and_upload_touch, sess_id, player_name, faults)

    return TouchResponse(
        chime_queued=True, running=running, touch_count=faults
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
    rank = get_board().add(sess_id, name, faults, seconds, score)
    entry = LeaderboardEntry(
        rank=rank, player_name=name, touch_count=faults,
        completion_time_seconds=seconds, score=score, session_id=sess_id
    )
    broadcaster.broadcast("game_complete", entry.model_dump())
    broadcaster.broadcast("session_update", {"active": False})
    return entry


@router.get("/leaderboard", response_model=List[LeaderboardEntry])
async def get_leaderboard(limit: int = 20):
    return get_board().top(max(1, min(limit, 200)))


@router.delete("/leaderboard")
async def clear_leaderboard():
    get_board().clear()
    broadcaster.broadcast("leaderboard_cleared", {})
    return {"cleared": True}
