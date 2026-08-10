#!/bin/sh
# Regenerate proto/skyweave.pb.{c,h} from v2/proto/skyweave.proto.
#
# The generated sources are CHECKED IN, exactly like the host's
# skyweave_pb2.py, so the documented build never needs a protoc toolchain.
# The bounds come from skyweave.options verbatim — that file is the ONE
# declaration both sides allocate from — so this script never edits it and
# never passes a bound on the command line.
#
# nanopb is pinned. Bumping it is a decision, not a maintenance step: the
# generated encoder's byte output is the D8 wire gate, and test E2 is what
# will tell you if a new version changed it.

set -eu

NANOPB_VERSION="0.4.9.1"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
PROTO_DIR="${HERE}/../../proto"

command -v uv >/dev/null 2>&1 || {
    echo "uv is required (it runs the pinned generator in a throwaway env)" >&2
    exit 1
}

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
cp "${PROTO_DIR}/skyweave.proto" "${PROTO_DIR}/skyweave.options" "${WORK}/"

( cd "${WORK}" && uv run \
    --with "nanopb==${NANOPB_VERSION}" \
    --with "grpcio-tools>=1.60" \
    --with "protobuf>=5.27" \
    python -m nanopb.generator.nanopb_generator skyweave.proto )

cp "${WORK}/skyweave.pb.c" "${WORK}/skyweave.pb.h" "${HERE}/proto/"
echo "regenerated ${HERE}/proto/skyweave.pb.{c,h} with nanopb ${NANOPB_VERSION}"
echo "now run the E2 tests: uv run pytest tests/edge/test_e2_nanopb_parity.py"
