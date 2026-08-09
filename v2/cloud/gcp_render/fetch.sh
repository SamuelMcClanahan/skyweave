#!/usr/bin/env bash
# Download render outputs from the bucket to the repo, verify hashes,
# then optionally delete the bucket (the last thing that costs money).
set -euo pipefail
PROJECT="${PROJECT:?set PROJECT=your-gcp-project-id}"
BUCKET="${BUCKET:-gs://${PROJECT}-skyweave-render}"
DEST="${DEST:-../../data/renders}"

mkdir -p "$DEST"
gsutil -m cp -r "$BUCKET/outputs/out/*" "$DEST/"
( cd "$DEST" && sha256sum -c SHA256SUMS )
echo ""
echo "Verified. To stop ALL remaining charges:"
echo "  gsutil -m rm -r $BUCKET"
