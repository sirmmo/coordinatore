FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY coordinatore ./coordinatore
COPY games ./games
RUN pip install .

# Persistent state — mount a volume on /data
ENV DB_PATH=/data/coordinatore.sqlite \
    GAMES_DIR=/app/games
VOLUME ["/data"]

# Token MUST be passed via env: -e TELEGRAM_BOT_TOKEN=...
ENTRYPOINT ["coordinatore"]
