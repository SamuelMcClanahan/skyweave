#!/usr/bin/env bash
# Run the drone SITL tier in a linux/amd64 container (README.md in this folder).
#
#   run.sh build                    build the image
#   run.sh fast                     drone fast tier (not slow, not sitl)
#   run.sh pytest ARGS...           uv run pytest ARGS (cwd v2/)
#   run.sh harness ARGS...          python -m skyweave2.drone.harness ARGS
#   run.sh gate OUT [ARGS...]       harness --seed-set gate --out OUT ARGS
#   run.sh shell [-c CMD]           bash in the container
#
# The repo is mounted at its own host path, so absolute host paths under the
# repo (e.g. <repo>/_local/...) work unchanged as --out. Nothing outside the
# repo is mounted. Every command first syncs dependencies into the venv volume
# (project not installed) and runs sitl.ensure_sitl() (download once, verify).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
IMAGE="${SKYWEAVE_SITL_IMAGE:-skyweave-sitl:py310}"
VENV_VOL="${SKYWEAVE_SITL_VENV_VOLUME:-skyweave-sitl-venv}"
CACHE_VOL="${SKYWEAVE_SITL_CACHE_VOLUME:-skyweave-sitl-cache}"
PLATFORM=linux/amd64

build() { docker build --platform "$PLATFORM" -t "$IMAGE" "$HERE"; }

in_container() {
  docker image inspect "$IMAGE" >/dev/null 2>&1 || build
  local tty=()
  if [ -t 0 ] && [ -t 1 ]; then tty=(-it); fi
  # shellcheck disable=SC2016  # expanded inside the container
  local prep='uv sync --frozen --extra dev --no-install-project --quiet \
    && python -c "from skyweave2.drone.sitl import ensure_sitl; ensure_sitl()" \
    && exec "$@"'
  # A git worktree keeps its git dir under the main checkout's .git, outside
  # $REPO. Mount that common dir read-only so harness.batch.versions() can
  # record `git rev-parse HEAD` in every scorecard.
  local git_mount=()
  local common
  if common="$(git -C "$REPO" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" \
    && [ -d "$common" ] && case "$common" in "$REPO"/*) false ;; *) true ;; esac; then
    git_mount=(-v "$common:$common:ro")
  fi
  docker run --rm --init ${tty[@]+"${tty[@]}"} --platform "$PLATFORM" \
    -v "$REPO:$REPO" ${git_mount[@]+"${git_mount[@]}"} -w "$REPO/v2" \
    -v "$VENV_VOL:/opt/venv" -v "$CACHE_VOL:/cache" \
    -e PYTHONPATH="$REPO/v2/src" \
    -e SKYWEAVE_HARNESS_JOBS -e SKYWEAVE_HARNESS_SPEEDUP \
    "$IMAGE" bash -c "$prep" bash "$@"
}

abs_under_repo() {
  local p="$1"
  case "$p" in /*) ;; *) p="$PWD/$p" ;; esac
  case "$p" in
    "$REPO"/*) printf '%s\n' "$p" ;;
    *) echo "error: $1 is not under $REPO (only the repo is mounted)" >&2; exit 2 ;;
  esac
}

cmd="${1:-}"
[ $# -gt 0 ] && shift
case "$cmd" in
  build) build ;;
  fast) in_container uv run pytest tests/drone -m "not slow and not sitl" -q "$@" ;;
  pytest) in_container uv run pytest "$@" ;;
  harness) in_container uv run python -m skyweave2.drone.harness "$@" ;;
  gate)
    [ $# -ge 1 ] || { echo "usage: run.sh gate OUT [ARGS...]" >&2; exit 2; }
    out="$(abs_under_repo "$1")"; shift
    in_container uv run python -m skyweave2.drone.harness --seed-set gate --out "$out" "$@"
    ;;
  shell) in_container bash "$@" ;;
  *) sed -n '2,14p' "$0" >&2; exit 2 ;;
esac
