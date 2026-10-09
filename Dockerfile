# Multi-stage build: dependencies resolved from uv.lock, no dev dependencies, non-root runtime.
FROM python:3.13-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.6.11 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.13-slim-bookworm
RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
# The synthetic catalog ships inside the package (inventory_mcp/data). The SQLite file
# lives in /tmp, which on Cloud Run is in-memory and ephemeral (SPEC-AMENDMENT-1, A3).
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    INVENTORY_HOST=0.0.0.0 \
    INVENTORY_DB_PATH=/tmp/restock_requests.db
USER app
EXPOSE 8080
# Exec form: the Python process is PID 1 and receives SIGTERM for graceful shutdown.
CMD ["python", "-m", "inventory_mcp"]
