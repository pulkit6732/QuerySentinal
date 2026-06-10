# QuerySentinel backend — Render/Cloud Run friendly image.
FROM python:3.11-slim

WORKDIR /app

# Build deps for any source builds (most wheels are prebuilt)
RUN apt-get update && apt-get install -y --no-install-recommends gcc g++ \
    && rm -rf /var/lib/apt/lists/*

# Python deps (cached layer)
COPY requirements.txt .
RUN pip install --upgrade pip && pip install --no-cache-dir -r requirements.txt

# App source
COPY . .

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Render/Cloud Run inject $PORT — bind to it (fallback 8080 for local docker run).
CMD ["sh", "-c", "python -m uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1 --loop asyncio --log-level info"]
