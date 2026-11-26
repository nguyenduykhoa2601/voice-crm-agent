# syntax=docker/dockerfile:1
# Voice CRM Agent: FastAPI + Playwright (Chromium headless shell) + Gemini Live.
# Sized for Render's free web service (512 MB RAM, 0.1 CPU).
FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.8.18 /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PATH=/opt/venv/bin:$PATH

WORKDIR /app

# Dependencies first (cached unless the lockfile changes). No dev group.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project \
    && rm -rf /root/.cache

# Only the Chromium headless shell (smaller than full Chromium) + its system libs.
RUN playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/* /tmp/*

# Unprivileged runtime user.
RUN useradd --create-home --uid 10001 app

COPY app ./app
COPY static ./static
COPY data/ui_map.json ./data/ui_map.json
RUN chown -R app:app /app/data

USER app

ENV RENDER=1 \
    HEADLESS=1 \
    MAX_SESSIONS=1 \
    LOW_MEMORY=1 \
    PORT=7860

EXPOSE 7860

# Single worker (sessions and the shared browser live in one process).
# WebSocket pings keep the voice socket alive through Render's proxy.
CMD ["sh", "-c", "exec uvicorn app.server:app --host 0.0.0.0 --port ${PORT:-7860} --workers 1 --ws-max-size 1048576 --ws-ping-interval 20 --ws-ping-timeout 30 --proxy-headers --forwarded-allow-ips='*' --timeout-graceful-shutdown 10"]
