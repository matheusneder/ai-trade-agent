# syntax=docker/dockerfile:1
FROM python:3.14-slim AS runtime

COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependencies first (cached layer while the lockfile does not change)
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

# Profiles, circuit breakers and analyst (in compose, config/ is mounted on top for changes without a rebuild)
COPY config ./config

RUN useradd --create-home --uid 10001 agent
USER agent

CMD ["trade-agent", "run"]
