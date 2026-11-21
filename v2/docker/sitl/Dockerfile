# linux/amd64 image for the drone SITL tier (pinned ArduCopter 4.7.0 SITL is
# x86-64 Linux only). On an Apple-silicon Mac it runs under Rosetta in colima.
#
# The image holds only Python 3.10, uv and git. The repo is bind-mounted at run
# time and the Python environment lives in a named volume at /opt/venv, so the
# host's arm64 v2/.venv is never touched. See README.md and run.sh.
FROM --platform=linux/amd64 python:3.10-slim

# git: harness.batch.versions() records `git rev-parse HEAD` in every scorecard.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system --add safe.directory '*'

COPY --from=ghcr.io/astral-sh/uv:0.11.17 /uv /uvx /usr/local/bin/

# UV_PROJECT_ENVIRONMENT: the env goes to /opt/venv, not <repo>/v2/.venv.
# UV_NO_SYNC: `uv run` never installs the project (an install would rewrite the
#   tracked v2/src/skyweave2.egg-info); run.sh syncs dependencies only.
# PYTHONPATH (set by run.sh to <repo>/v2/src) makes skyweave2 importable, and
#   the tracked egg-info there supplies importlib.metadata.version("skyweave2").
# PYTHONDONTWRITEBYTECODE / PYTEST_ADDOPTS: no __pycache__ or .pytest_cache
#   written into the mounted tree.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_CACHE_DIR=/cache/uv \
    UV_LINK_MODE=copy \
    UV_PYTHON=/usr/local/bin/python3.10 \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_SYNC=1 \
    SKYWEAVE_SITL_DIR=/cache/sitl-copter-4.7.0 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTEST_ADDOPTS="-p no:cacheprovider" \
    PATH=/opt/venv/bin:$PATH
