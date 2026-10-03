# One image for both Railway services:
#   API    -> default CMD below (runs migrations, then uvicorn)
#   worker -> start command overridden to: python -m app.worker.run
# Python version matches the one the test suite ran on.
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# ffmpeg comes from the imageio-ffmpeg wheel, so no apt-get is needed.

COPY alembic.ini .
COPY alembic ./alembic
COPY app ./app

# Railway provides $PORT.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
