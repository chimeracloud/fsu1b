# FSU1B — Betfair Exchange Gateway
# Phase 1 (shell). Standard Cloud Run image, Python 3.12 slim.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY main.py ./
COPY core/ ./core/
COPY services/ ./services/
COPY models/ ./models/
COPY resources/ ./resources/

# Cloud Run injects PORT.
ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
