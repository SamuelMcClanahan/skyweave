#!/usr/bin/env bash
# Set up the instructional video pipeline on Ubuntu/Debian.
# Usage: ./setup.sh            (system packages + Python env + Kokoro voice model)
#        ./setup.sh --latex    (also install a LaTeX subset so Manim MathTex works)
set -euo pipefail
cd "$(dirname "$0")"

WANT_LATEX=0
for a in "$@"; do
  case "$a" in
    --latex) WANT_LATEX=1 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

SUDO=""
if [ "$(id -u)" -ne 0 ]; then SUDO="sudo"; fi

# 1. System packages. Manim needs Pango/Cairo headers (manimpango has no Linux wheel) and ffmpeg.
PKGS=(libpango1.0-dev libcairo2-dev pkg-config ffmpeg)
if [ "$WANT_LATEX" -eq 1 ]; then
  PKGS+=(texlive-latex-base texlive-latex-extra texlive-fonts-recommended texlive-science dvisvgm cm-super)
fi
MISSING=()
for p in "${PKGS[@]}"; do
  dpkg -s "$p" >/dev/null 2>&1 || MISSING+=("$p")
done
if [ "${#MISSING[@]}" -gt 0 ]; then
  echo "installing: ${MISSING[*]}"
  $SUDO apt-get update -q
  DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y -q "${MISSING[@]}"
fi

# 2. Python environment (uv reads pyproject.toml and .python-version).
command -v uv >/dev/null || { echo "uv is required: https://docs.astral.sh/uv/" >&2; exit 1; }
uv sync --extra dev

# 3. Kokoro-82M ONNX voice model (Apache-2.0). Verified by SHA-256.
mkdir -p models
fetch() { # name sha256
  local name="$1" sum="$2" dst="models/$1"
  if [ -f "$dst" ] && echo "$sum  $dst" | sha256sum -c --quiet - 2>/dev/null; then return 0; fi
  for url in \
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/$name" \
    "https://huggingface.co/fastrtc/kokoro-onnx/resolve/main/$name"; do
    echo "downloading $name from $url"
    if curl -fsSL -o "$dst.part" "$url" && echo "$sum  $dst.part" | sha256sum -c --quiet -; then
      mv "$dst.part" "$dst"; return 0
    fi
    rm -f "$dst.part"
  done
  echo "could not download $name with the expected checksum" >&2; return 1
}
fetch kokoro-v1.0.onnx 7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5
fetch voices-v1.0.bin  bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d

uv run ivideo doctor
