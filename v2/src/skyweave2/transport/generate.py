"""Compile ``proto/skyweave.proto`` into the checked-in descriptor module.

Regeneration is an explicit, rare act — the generated module is committed so
the documented `uv sync --extra dev` + `uv run pytest` path never needs a
protoc toolchain. Run this only when `proto/skyweave.proto` changes:

    cd v2
    uv sync --extra dev --extra proto
    uv run python -m skyweave2.transport.generate

Test W1 parses the `.proto` text independently and compares it against the
compiled descriptor, so forgetting to regenerate fails loudly rather than
letting the schema and the codec drift apart.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

V2_ROOT = Path(__file__).resolve().parents[3]
PROTO_DIR = V2_ROOT / "proto"
PROTO_FILE = PROTO_DIR / "skyweave.proto"
OUT_DIR = Path(__file__).resolve().parent / "proto"


def generate() -> Path:
    """Run protoc; return the path of the generated module."""
    try:
        import grpc_tools.protoc  # noqa: F401
    except ImportError as exc:  # pragma: no cover - depends on the `proto` extra
        raise SystemExit(
            "grpcio-tools is not installed. Regeneration needs it:\n"
            "    uv sync --extra dev --extra proto"
        ) from exc

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "__init__.py").touch()
    # `-I proto` keeps the descriptor's embedded file name "skyweave.proto"
    # (not an absolute path), so the generated module is machine-independent.
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "grpc_tools.protoc",
            f"--proto_path={PROTO_DIR}",
            f"--python_out={OUT_DIR}",
            "skyweave.proto",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"protoc failed:\n{result.stdout}\n{result.stderr}")
    generated = OUT_DIR / "skyweave_pb2.py"
    if not generated.exists():
        raise SystemExit(f"protoc reported success but {generated} is missing")
    return generated


def main() -> None:
    path = generate()
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
