# ---- Build stage: install Python dependencies ----
FROM python:3.13-slim AS builder

ARG INSTALL_BROWSER=false

RUN pip install --no-cache-dir uv

ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

RUN uv venv "$VIRTUAL_ENV"

COPY pyproject.toml uv.lock /app/

# Default: core deps only.  INSTALL_BROWSER=true adds playwright + camoufox.
RUN if [ "$INSTALL_BROWSER" = "true" ]; then \
      uv sync --frozen --no-dev --no-install-project --active --extra browser; \
    else \
      uv sync --frozen --no-dev --no-install-project --active; \
    fi

# Strip __pycache__ / .pyc from venv
RUN find "$VIRTUAL_ENV" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true


# ---- Runtime stage ----
FROM python:3.13-slim

ARG INSTALL_BROWSER=false

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    VIRTUAL_ENV=/opt/venv

ENV PATH="$VIRTUAL_ENV/bin:$PATH"

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy pre-built venv from builder (no uv / pip / setuptools bloat)
COPY --from=builder /opt/venv /opt/venv

# When INSTALL_BROWSER=true, pre-install Playwright Chromium + OS deps so
# the auto-registration solver works out of the box.
# Without this, the solver will attempt a lazy install on first run
# (requires network + root inside the container).
#   docker build --build-arg INSTALL_BROWSER=true -t grok2api:full .
RUN if [ "$INSTALL_BROWSER" = "true" ]; then \
      python -m playwright install --with-deps chromium \
      && rm -rf /var/lib/apt/lists/* /tmp/*; \
    fi

COPY config.defaults.toml /app/config.defaults.toml
COPY app /app/app
COPY main.py /app/main.py
COPY scripts /app/scripts

# When building on Windows, shell scripts may be copied with CRLF endings and
# without executable bit.  Normalize both to keep ENTRYPOINT reliable.
RUN sed -i 's/\r$//' /app/scripts/*.sh || true \
    && chmod +x /app/scripts/*.sh || true

RUN mkdir -p /app/data /app/data/tmp /app/logs

EXPOSE 8000

ENTRYPOINT ["/app/scripts/entrypoint.sh"]
CMD ["/app/scripts/start.sh"]
