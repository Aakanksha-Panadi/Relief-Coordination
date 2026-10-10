# RescueIQ backend - Cloud Run container
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so layer caching survives source edits.
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./
# The orchestrator reads the road graph from ../data at runtime.
COPY data/ /data/

ENV ROAD_GRAPH_PATH=/data/road_graph.json \
    PORT=8080

EXPOSE 8080

# Cloud Run injects $PORT; honour it rather than hardcoding.
CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}
