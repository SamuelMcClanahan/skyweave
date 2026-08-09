# GCP L4 render kit (EXP-001 clips)

Renders the gate clip and the target-free negative clip on a GCP L4 VM that
cannot keep charging you.

## Cost-stop guarantees, in order of trust

1. `--max-run-duration` + `--instance-termination-action=DELETE`: GCP deletes
   the VM after the cap (default 6 h) no matter what happens inside it. This
   holds even if the render hangs, the script crashes, or your laptop is off.
2. The startup script deletes its own instance the moment outputs are
   uploaded (or on any failure, after shipping logs).
3. The boot disk is deleted with the instance (GCE default).
4. The only survivor is the GCS bucket. `fetch.sh` prints the one command
   that removes it after you download.

Rough cost (Modeled, not a quote): L4 spot ~2 h of render is on the order of
a few dollars; the hard cap bounds the worst case at cap-hours times the
hourly rate. Egress of the EXR outputs (~50 GB both clips) is the largest
single line unless you enable the on-VM postprocess hook to ship U8 only.

## Usage

```bash
cd v2/cloud/gcp_render
PROJECT=your-project ./launch.sh          # SPOT=0 for on-demand, MAX_RUN=4h to tighten the cap
# ... watch serial output if curious; VM deletes itself when done ...
PROJECT=your-project ./fetch.sh           # downloads + verifies SHA256SUMS
gsutil -m rm -r gs://your-project-skyweave-render   # last cost off
```

Requires: gcloud CLI authenticated, a billing-enabled project, L4 quota in
the chosen zone (`ZONE=` to change).

## Prerequisite code change (D4 prep)

`v2/blender/exp001_generate.py` must accept `--no-target` (render the frozen
scene with the target disabled) and `--device OPTIX`. If either flag is
missing, add them as the first D4 task before launching.

## Determinism note (recorded in the decisions log)

Golden clips are DEFINED as the artifacts of this run: the dataset manifests
record device (L4/OptiX), driver, and Blender build (5.2.0), and
`SHA256SUMS` pins the bytes. CPU renders and other GPUs are benchmarks, not
reference data. Spot preemption mid-render is safe: outputs only upload on
completion, so a preempted run is simply relaunched.

## Operational notes from the 2026-08-06 run (Measured)

- L4 spot capacity: us-west1 zones were stocked out; us-east1-b had stock.
- Driver: the DLVM pinned-driver path can fail DKMS against current kernels;
  the working fix is an Ubuntu image with the prebuilt signed NVIDIA modules
  (`linux-modules-nvidia-*-gcp`) instead of DKMS builds.
- Spot preemption mid-render is survivable with GCS checkpointing and atomic
  frame writes (--skip-existing); two preemptions cost only relaunch time.
- Render rate: ~1.6 s/frame on L4/OptiX at 128 samples (vs 11.6 s M4 CPU).
- Total cost of the full golden render campaign: ~$2.
