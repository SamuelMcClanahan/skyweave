#!/usr/bin/env bash
# Launch a self-deleting GCP L4 render VM for the EXP-001 clips.
# Cost safety: --max-run-duration + --instance-termination-action=DELETE is a
# HARD cap enforced by GCP even if everything below fails. The startup script
# additionally self-deletes on completion.
set -euo pipefail

# ---- fill these in ---------------------------------------------------------
PROJECT="${PROJECT:?set PROJECT=your-gcp-project-id}"
ZONE="${ZONE:-us-central1-a}"            # any zone with L4 (g2) capacity
BUCKET="${BUCKET:-gs://${PROJECT}-skyweave-render}"
MAX_RUN="${MAX_RUN:-6h}"                 # hard kill-and-delete cap
SPOT="${SPOT:-1}"                        # 1 = spot pricing (~60-70% cheaper, can be preempted)
# ----------------------------------------------------------------------------

NAME="skyweave-exp001-render"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"     # skyweave-main

gcloud config set project "$PROJECT" >/dev/null

# Bucket + inputs
gsutil ls "$BUCKET" >/dev/null 2>&1 || gsutil mb -l "${ZONE%-*}" "$BUCKET"
tar -C "$REPO" -czf /tmp/skyweave_v2_src.tgz v2/src v2/blender v2/configs v2/pyproject.toml v2/uv.lock
gsutil -m cp /tmp/skyweave_v2_src.tgz "$HERE/startup.sh" "$BUCKET/inputs/"

PROVISIONING=()
if [ "$SPOT" = "1" ]; then
  PROVISIONING=(--provisioning-model=SPOT --instance-termination-action=DELETE)
fi

gcloud compute instances create "$NAME" \
  --zone="$ZONE" \
  --machine-type=g2-standard-8 \
  --accelerator=type=nvidia-l4,count=1 \
  --image-family=common-gpu-debian-11-py310 \
  --image-project=deeplearning-platform-release \
  --boot-disk-size=200GB --boot-disk-type=pd-balanced \
  --maintenance-policy=TERMINATE \
  --max-run-duration="$MAX_RUN" \
  --instance-termination-action=DELETE \
  "${PROVISIONING[@]}" \
  --scopes=storage-rw,compute-rw \
  --metadata=startup-script-url="$BUCKET/inputs/startup.sh",render-bucket="$BUCKET",install-nvidia-driver=True

echo ""
echo "Launched. Hard cost cap: instance self-DELETES after $MAX_RUN regardless of outcome."
echo "Watch progress:  gcloud compute instances get-serial-port-output $NAME --zone=$ZONE | tail -50"
echo "When done, the instance is gone and results are in: $BUCKET/outputs/"
echo "Fetch with: ./fetch.sh"
