FROM python:3.11-slim

# System dependencies for Pillow, Pygame, and fonts
RUN apt-get update && apt-get install -y --no-install-recommends \
    libsdl2-mixer-2.0-0 \
    libsdl2-2.0-0 \
    fonts-dejavu-core \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Create persistent directories for SQLite and media
RUN mkdir -p /app/data /app/media

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/main.py .

# Environment defaults for k3s
ENV PORT=8000 \
    DB_PATH=/app/data/leaderboard.db \
    MEDIA_DIR=/app/media \
    SDL_AUDIODRIVER=dummy \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# IMPORTANT: Keep --workers 1 because GameState is held in-memory
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]