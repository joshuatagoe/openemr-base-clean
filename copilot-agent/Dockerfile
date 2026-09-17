# Clinical Co-Pilot agent service (ARCHITECTURE.md section 14, Railway service B).
# Stateless per replica; bundles live in memory for a bounded TTL. No PHI on disk.
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /srv/copilot-agent

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv

# Dependencies first so source edits do not invalidate the layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY fixtures ./fixtures

RUN useradd --system --uid 10001 --no-create-home copilot
USER copilot

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD ["/srv/copilot-agent/.venv/bin/python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"]

CMD ["/srv/copilot-agent/.venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--no-server-header"]
