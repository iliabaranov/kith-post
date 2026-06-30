# Kith Invite — single image, same locally and on the home server.
FROM python:3.12-slim

# uv for fast, reproducible installs
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    KITH_DATA_DIR=/data

WORKDIR /app

# Install deps first (cached layer), then the project.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
RUN uv sync --frozen --no-dev

# Run as non-root; /data is a mounted volume.
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /data && chown -R app:app /app /data
USER app
VOLUME ["/data"]
EXPOSE 8000

CMD ["uv", "run", "--no-dev", "uvicorn", "kith.web.app:app", "--host", "0.0.0.0", "--port", "8000"]
