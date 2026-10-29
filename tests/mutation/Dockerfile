# syntax=docker/dockerfile:1.28.0
# Linux runner of the mutation tests (tests/mutation/README.md). mutmut forks a warm process per
# mutant, which Windows cannot do: `uv run python -m scripts.mutation` builds this image and runs
# the fronts in it. Same Python and uv as the CI; the dependencies of the lockfile with the dev and
# mutation groups; the Docker CLI and Compose for the containers the tests start (testcontainers,
# the smoke stacks), as siblings on the host's daemon; Chromium for the smoke tests.
FROM python:3.15.1-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /uvx /bin/
COPY --from=docker:29.5.3-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker:29.5.3-cli /usr/local/libexec/docker/cli-plugins/docker-compose \
    /usr/local/libexec/docker/cli-plugins/docker-compose

# git: the legacy regression test reads its base commit with `git archive` (the runner mounts the
# repository's git directory read-only, owned by another user)
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && git config --system safe.directory '*' && rm -rf /var/lib/apt/lists/*

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /opt/deps
# only the dependencies: the code comes from the repository at run time (the project is
# installed from the work copy, editable, when a run starts)
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-install-project --group mutation
RUN playwright install --with-deps chromium && rm -rf /var/lib/apt/lists/*
