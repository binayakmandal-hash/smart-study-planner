# Smart Study Planner - container for Render (or any Docker host)
FROM python:3.12-slim

# System libraries needed by OpenCV / ONNX Runtime (used by RapidOCR)
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STUDYPLANNER_DEMO=1 \
    STUDYPLANNER_DB=/app/data/database.db \
    STUDYPLANNER_OCR_DPI=130 \
    OMP_NUM_THREADS=2

WORKDIR /app
COPY requirements-render.txt .
RUN pip install --no-cache-dir -r requirements-render.txt

COPY . .
RUN mkdir -p /app/data /app/uploads /app/ocr_cache

# One worker keeps background OCR jobs and their progress in the same process.
CMD gunicorn app:app --workers 1 --threads 4 --timeout 300 --bind 0.0.0.0:${PORT:-10000}
