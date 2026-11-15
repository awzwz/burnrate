# syntax=docker/dockerfile:1
# Burn Rate back end: ONE image for every online service, worker, loop and job.
# The role is chosen at run time by $SERVICE (burnrate/run.py); every setting arrives as a BR_* variable.
# Nothing secret is copied in: .dockerignore keeps .env, deploy/secrets and .data out of the build context.
#
#   docker buildx build --platform linux/amd64 -t burnrate-api .
#   docker run --rm -e BR_MODE=local -e BR_ENV=dev -p 8000:8000 burnrate-api

ARG PYTHON_IMAGE=python:3.12-slim
# Same uv as on the laptops that write uv.lock (uv --version), so `--locked` reads the lock file the same way.
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.1

FROM ${UV_IMAGE} AS uv

# ---- dependencies: a virtualenv built from uv.lock, without the project itself --------------------------------
FROM ${PYTHON_IMAGE} AS deps
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3.12
WORKDIR /src
COPY pyproject.toml uv.lock ./
# --locked fails the build when pyproject.toml and uv.lock disagree (somebody added a dependency and did not
# run `uv lock`), instead of quietly shipping an image without it.
RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    uv sync --locked --no-dev --no-install-project

# ---- runtime ---------------------------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime
# tini is PID 1: the workers and jobs are plain `asyncio.run(...)` with no SIGTERM handler, and the kernel does
# not deliver an unhandled SIGTERM to PID 1. Without tini every worker pod would hang for its whole
# terminationGracePeriodSeconds on each rollout. uvicorn (online services) still gets the signal and drains.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 burnrate \
 && useradd --system --uid 10001 --gid 10001 --home-dir /app --no-create-home --shell /usr/sbin/nologin burnrate \
 && mkdir -p /app /data \
 && chown 10001:10001 /data
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    PORT=8000 \
    BR_DATA_DIR=/data
WORKDIR /app
COPY --from=deps /opt/venv /opt/venv
# The source tree is copied as is (not installed as a wheel) so that any non-Python file inside the package
# ships too, and local mode finds the same relative paths as on a laptop.
COPY burnrate ./burnrate
# The cluster runs the container with a read-only root filesystem: compile the bytecode now.
RUN python -m compileall -q burnrate
USER 10001:10001
EXPOSE 8000
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "burnrate.run"]
