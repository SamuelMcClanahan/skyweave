#!/usr/bin/env bash
# Make an offline install bundle for the Cubie A7S (contract B-D3).
# Run this on a computer that has internet, git and uv (macOS or Linux).
# It does not run on the board and it does not connect to the board.
#
#   v2/deploy/cubie/make-bundle.sh [--out DIR] [--extras LIST]
#
# The bundle is one .tar.gz file with all that install.sh needs:
#   - the drone stack code of the commit HEAD (git archive; changes that are
#     not committed are NOT in the bundle),
#   - a CPython 3.10 for linux aarch64 (the uv-managed build),
#   - the wheels for the packages that v2/uv.lock pins, for linux aarch64,
#   - requirements.txt with hashes (uv export from v2/uv.lock),
#   - this deploy directory (install.sh, service files, README).
# Default --out: the directory _local/bundles in the repository, if git
# ignores it there. Default --extras: drone.
set -euo pipefail
export LC_ALL=C

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXTRAS=drone
OUT_DIR=""
PYTHON_REQUEST=cpython-3.10-linux-aarch64-gnu
# pip tags for the board: CPython 3.10, glibc aarch64. Debian 13 has glibc
# 2.41; wheels built for an older glibc are accepted.
GLIBC_TAGS="17 24 26 27 28 31 34 35 36 39"

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}
info() { printf '==> %s\n' "$*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --out)
      [ $# -ge 2 ] || die "--out needs a directory."
      OUT_DIR=$2
      shift 2
      ;;
    --extras)
      [ $# -ge 2 ] || die "--extras needs a list, for example: drone,percepd"
      EXTRAS=$2
      shift 2
      ;;
    -h | --help)
      sed -n '2,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
done

for tool in git uv tar; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is not installed on this computer."
done
if command -v sha256sum >/dev/null 2>&1; then
  sha256() { sha256sum "$@"; }
elif command -v shasum >/dev/null 2>&1; then
  sha256() { shasum -a 256 "$@"; }
else
  die "sha256sum or shasum is necessary."
fi

REPO=$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel) || die "$SCRIPT_DIR is not in a git repository."
REVISION=$(git -C "$REPO" rev-parse HEAD)
SHORT=$(git -C "$REPO" rev-parse --short=12 HEAD)
if [ -n "$(git -C "$REPO" status --porcelain -- v2/src v2/pyproject.toml v2/uv.lock v2/deploy/cubie)" ]; then
  printf 'WARNING: there are changes that are not committed. They are NOT in the bundle.\n' >&2
  printf 'WARNING: the bundle has the commit %s only.\n' "$SHORT" >&2
fi
if [ -z "$OUT_DIR" ]; then
  OUT_DIR=$REPO/_local/bundles
  git -C "$REPO" check-ignore -q "$OUT_DIR/x" ||
    die "git does not ignore $OUT_DIR here. Give a directory with --out DIR."
fi
mkdir -p "$OUT_DIR"
OUT_DIR=$(cd "$OUT_DIR" && pwd)

NAME=skyweave-cubie-bundle-$SHORT
WORK=$(mktemp -d "${TMPDIR:-/tmp}/skyweave-bundle.XXXXXX")
trap 'rm -rf "$WORK"' EXIT
TREE=$WORK/tree
BUNDLE=$WORK/$NAME
mkdir -p "$TREE" "$BUNDLE/wheels" "$BUNDLE/python" "$BUNDLE/src" "$BUNDLE/deploy"

info "Code: commit $REVISION"
git -C "$REPO" archive --format=tar HEAD v2/src v2/pyproject.toml v2/uv.lock v2/deploy/cubie |
  tar -x -C "$TREE"
[ -d "$TREE/v2/src/skyweave2/drone" ] || die "the commit has no v2/src/skyweave2/drone."
cp -R "$TREE/v2/src/skyweave2" "$BUNDLE/src/"
if [ -d "$TREE/v2/src/skyweave2.egg-info" ]; then
  cp -R "$TREE/v2/src/skyweave2.egg-info" "$BUNDLE/src/"
fi
cp "$TREE/v2/pyproject.toml" "$TREE/v2/uv.lock" "$BUNDLE/"
cp -R "$TREE/v2/deploy/cubie/." "$BUNDLE/deploy/"

info "Locked requirements: uv export from uv.lock (extras: $EXTRAS)"
extra_args=()
IFS=, read -r -a extras_list <<<"$EXTRAS"
for extra in ${extras_list[@]+"${extras_list[@]}"}; do
  if [ -n "$extra" ]; then extra_args+=(--extra "$extra"); fi
done
uv export --quiet --project "$TREE/v2" --frozen --no-dev --no-emit-project --no-header \
  --no-annotate --format requirements-txt ${extra_args[@]+"${extra_args[@]}"} \
  -o "$WORK/requirements.universal.txt"
# uv.lock is for all platforms. Keep the lines that apply to the board and
# remove their markers, so that pip on this computer (a different OS and
# Python) downloads exactly the board's set. The hashes stay those of uv.lock.
uv run --quiet --no-project --python 3.10 --with packaging python - \
  "$WORK/requirements.universal.txt" "$BUNDLE/requirements.txt" <<'PY'
import sys

from packaging.markers import Marker

BOARD = {
    "implementation_name": "cpython",
    "implementation_version": "3.10.0",
    "os_name": "posix",
    "platform_machine": "aarch64",
    "platform_python_implementation": "CPython",
    "platform_release": "",
    "platform_system": "Linux",
    "platform_version": "",
    "python_full_version": "3.10.0",
    "python_version": "3.10",
    "sys_platform": "linux",
}
src, dst = sys.argv[1:3]
entries, cur = [], []
for raw in open(src, encoding="utf-8"):
    line = raw.rstrip("\n")
    if not line.strip() or line.lstrip().startswith("#"):
        continue
    cur.append(line)
    if not line.endswith("\\"):
        entries.append(cur)
        cur = []
if cur:
    entries.append(cur)
out = []
for entry in entries:
    head = entry[0]
    cont = head.endswith("\\")
    spec = head[:-1].rstrip() if cont else head
    if ";" in spec:
        spec, marker = (part.strip() for part in spec.split(";", 1))
        if not Marker(marker).evaluate(BOARD):
            print(f"    not for the board, left out: {spec} ; {marker}")
            continue
    if "==" not in spec:
        sys.exit(f"ERROR: requirement without an exact version: {spec}")
    out.append(spec + (" \\" if cont else ""))
    out.extend(entry[1:])
with open(dst, "w", encoding="utf-8") as fh:
    fh.write("\n".join(out) + "\n")
PY

info "Wheels: download for CPython 3.10, linux aarch64 (hashes checked)"
platform_args=(--platform manylinux2014_aarch64)
for minor in $GLIBC_TAGS; do
  platform_args+=(--platform "manylinux_2_${minor}_aarch64")
done
# A package with no wheel for the board stops the script here. That is
# intended: the board has no compiler set-up for source builds.
uv tool run --quiet pip download --quiet --disable-pip-version-check \
  --require-hashes --only-binary=:all: --no-deps \
  --implementation cp --python-version 3.10 --abi cp310 --abi abi3 --abi none \
  "${platform_args[@]}" -r "$BUNDLE/requirements.txt" -d "$BUNDLE/wheels"

info "Python: uv-managed CPython 3.10 for linux aarch64"
UV_PYTHON_INSTALL_DIR="$WORK/python" uv python install --quiet --no-bin "$PYTHON_REQUEST"
PYTHON_DIR=""
for d in "$WORK"/python/cpython-3.10.*-linux-aarch64-gnu; do
  if [ -d "$d" ] && [ ! -L "$d" ]; then PYTHON_DIR=$(basename "$d"); fi
done
[ -n "$PYTHON_DIR" ] || die "uv did not install $PYTHON_REQUEST."
tar -C "$WORK/python" -cf - "$PYTHON_DIR" | tar -C "$BUNDLE/python" -xf -

cat >"$BUNDLE/BUNDLE_MANIFEST" <<EOF
format=skyweave-cubie-bundle-1
target=linux-aarch64-gnu
revision=$REVISION
build_epoch=$(date +%s)
python_dir=$PYTHON_DIR
extras=$EXTRAS
uv=$(uv --version)
EOF

info "Pack"
tar_args=()
if tar --version 2>/dev/null | grep -q bsdtar; then
  tar_args=(--no-xattrs --no-mac-metadata) # macOS: no ._ files, no xattr headers
fi
COPYFILE_DISABLE=1 tar -C "$WORK" ${tar_args[@]+"${tar_args[@]}"} -czf "$OUT_DIR/$NAME.tar.gz" "$NAME"
(cd "$OUT_DIR" && sha256 "$NAME.tar.gz" >"$NAME.tar.gz.sha256")

cat <<EOF

Bundle:   $OUT_DIR/$NAME.tar.gz
Checksum: $OUT_DIR/$NAME.tar.gz.sha256
Contents: commit $SHORT, $PYTHON_DIR, $(find "$BUNDLE/wheels" -name '*.whl' | wc -l | tr -d ' ') wheels (extras: $EXTRAS)

Next (README.md, "Copy to the board"): copy the two files to the board,
extract the .tar.gz there, and run  sudo $NAME/deploy/install.sh
EOF
