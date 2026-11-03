FROM python:3.11-slim

# Fonts for the photo banner (Pillow) and curl for health checks
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-dejavu-core \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Create persistent directories for SQLite and media
RUN mkdir -p /app/data /app/media

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/main.py backend/config.py backend/helpers.py backend/routes.py backend/schemas.py ./
COPY backend/templates/ ./templates/

# Environment defaults for k3s
ENV PORT=8000 \
    DB_PATH=/app/data/leaderboard.db \
    MEDIA_DIR=/app/media \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# IMPORTANT: Keep --workers 1 because GameState is held in-memory
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]