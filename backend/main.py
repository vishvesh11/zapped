"""Buzz-wire game backend (FastAPI) - Headscale Camera + Garage S3 + Dual Web UI.

Application assembly only. Configuration lives in ``config.py``, shared state and
infrastructure in ``helpers.py``, request/response models in ``schemas.py`` and the
endpoints in ``routes.py``.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from config import settings
from helpers import close_board, ensure_default_chime, init_board
from routes import public_router, router


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_default_chime()
    init_board()
    yield
    close_board()


app = FastAPI(title="Buzz Wire Game API", version="2.2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---- HTML Frontend Routes ------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
async def serve_public_ui():
    return (settings.templates_dir_resolved / "index.html").read_text(encoding="utf-8")


@app.get("/admin", response_class=HTMLResponse)
async def serve_admin_ui():
    return (settings.templates_dir_resolved / "admin.html").read_text(encoding="utf-8")


@app.get("/health")
async def health():
    return {"status": "ok"}


app.include_router(router)
app.include_router(public_router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=settings.port)
