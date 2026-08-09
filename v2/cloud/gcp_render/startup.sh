#!/usr/bin/env bash
# Runs ON the VM. Renders the gate clip and the negative clip with Blender
# 5.2.0 headless (CUDA/OptiX), uploads outputs + logs to GCS, then deletes
# its own instance. The launch-side --max-run-duration remains the hard cap.
set -uxo pipefail

BUCKET="$(curl -s -H 'Metadata-Flavor: Google' \
  'http://metadata.google.internal/computeMetadata/v1/instance/attributes/render-bucket')"
NAME="$(hostname)"
ZONE="$(curl -s -H 'Metadata-Flavor: Google' \
  'http://metadata.google.internal/computeMetadata/v1/instance/zone' | awk -F/ '{print $NF}')"

self_destruct() {
  gsutil -m cp /var/log/render*.log "$BUCKET/outputs/logs/" || true
  gcloud compute instances delete "$NAME" --zone="$ZONE" --quiet
}
trap self_destruct EXIT

cd /opt
# Blender 5.2.0 pinned (D3 decision). Verify the URL against blender.org if it 404s.
curl -fL -o blender.tar.xz \
  https://download.blender.org/release/Blender5.2/blender-5.2.0-linux-x64.tar.xz
tar xf blender.tar.xz && mv blender-5.2.0-linux-x64 blender
apt-get update -y && apt-get install -y libxi6 libxxf86vm1 libxfixes3 libxrender1 libgl1 libsm6

gsutil cp "$BUCKET/inputs/skyweave_v2_src.tgz" . && mkdir -p work && tar -C work -xzf skyweave_v2_src.tgz
cd work

# Gate clip (target on) and negative clip (target off).
# exp001_generate.py must support --no-target (added in D4 prep).
/opt/blender/blender -b --python v2/blender/exp001_generate.py -- \
  --manifest v2/configs/exp001_scene.yaml --device OPTIX --out /out/gate \
  > /var/log/render_gate.log 2>&1

/opt/blender/blender -b --python v2/blender/exp001_generate.py -- \
  --manifest v2/configs/exp001_scene.yaml --device OPTIX --out /out/negative --no-target \
  > /var/log/render_negative.log 2>&1

# Optional on-VM postprocess hook (e.g. stage-B U8 conversion to shrink egress).
# Set POSTPROCESS_CMD in this file before upload if desired; default: ship EXRs.
if [ -n "${POSTPROCESS_CMD:-}" ]; then
  bash -c "$POSTPROCESS_CMD" > /var/log/render_post.log 2>&1
fi

# Hash everything, then upload.
( cd /out && find . -type f -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS )
gsutil -m cp -r /out "$BUCKET/outputs/"
echo DONE > /tmp/done && gsutil cp /tmp/done "$BUCKET/outputs/DONE"
# trap runs: logs upload + self-delete
