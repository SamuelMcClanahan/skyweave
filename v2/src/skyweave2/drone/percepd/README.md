# percepd v1

Contract: `v2/docs/DRONE_CONTRACTS_D0.md` §11.4 **B-D2**, [P1], [C1], [C2], [C10].

percepd reads camera frames, runs an object detector, and sends one [P1]
detection packet per processed frame to the companion process (UDP, default
`127.0.0.1:14601`). The companion's tracker makes tracks from the boxes
(E2-D5).

This version proves that the chain camera -> detector -> boxes -> tracker
works on the bench. It uses a generic COCO model on the CPU. The balloon
model and the NPU backend come later. The CPU speed is not the flight number.
Every number in this folder is Provisional. Nothing here is Measured.

## Install

`onnxruntime` is the optional extra `percepd`:

```bash
cd v2
uv sync --extra percepd          # or: pip install onnxruntime
```

The rest of the drone stack and its tests do not need it. percepd imports it
only when the ONNX backend starts.

## The test model

The repo holds no model weights. Get the model once and keep it outside git
(for example in `_local/models/`, which git ignores).

| Item | Value |
| --- | --- |
| Model | YOLOX-Tiny, COCO, 80 classes, input 416 x 416 |
| File | `yolox_tiny.onnx`, 20 219 662 bytes |
| Source | `https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_tiny.onnx` (the official YOLOX release) |
| SHA-256 | `427cc366d34e27ff7a03e2899b5e3671425c262ea2291f88bb942bc1cc70b0f7` (the file downloaded on 2026-11-27) |
| License | Apache-2.0 (the YOLOX project license) |
| Layout | `--layout yolox` (the default) |

```bash
mkdir -p _local/models
curl -L -o _local/models/yolox_tiny.onnx \
  https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_tiny.onnx
```

The same release has `yolox_nano.onnx` (smaller, less accurate) and
`yolox_s.onnx` (640 x 640, more accurate, slower). They use the same layout.

Ultralytics models (YOLOv8, YOLO11, YOLO26) are AGPL-3.0 and need an export
step with the `ultralytics` package. That package and `torch` are not
dependencies of this repo. Export on another machine, then use
`--layout ultralytics` (output `(1, 4 + classes, N)`) or
`--layout ultralytics_e2e` (output `(1, N, 6)`, NMS in the graph). These two
decoders are tested on synthetic tensors only. Check the output shape of a
real export before you trust them.

## Run

On the board, with the flight camera mode (YUYV 1920 x 1200 at 80 fps are the
defaults):

```bash
python -m skyweave2.drone.percepd --camera /dev/video0 \
    --model /path/to/yolox_tiny.onnx
```

With an image folder or a video file (bench, CI):

```bash
python -m skyweave2.drone.percepd --images /path/to/images --loop \
    --model _local/models/yolox_tiny.onnx
python -m skyweave2.drone.percepd --video clip.mp4 \
    --model _local/models/yolox_tiny.onnx
```

A stable device name such as `/dev/v4l/by-id/...` is better than
`/dev/video0`: the number can change when the camera is plugged in again.
`v4l2-ctl --list-formats-ext -d /dev/video0` lists the modes the camera
offers. percepd logs the mode it asked for and the mode it got.

Without a model (camera, stamps and UDP link only; every packet has zero
boxes):

```bash
python -m skyweave2.drone.percepd --camera /dev/video0 --backend null
```

`--help` lists every option. The important ones:

| Option | Default | Meaning |
| --- | --- | --- |
| `--camera DEV`, `--video FILE`, `--images PATH` | one is required | The frame source |
| `--width`, `--height`, `--pixfmt`, `--fps` | 1920, 1200, YUYV, 80 | Camera mode. For file sources `--fps` is the pacing rate (default 10, 0 = no pacing) |
| `--loop` | off | File sources: start over at the end |
| `--every-frame` | off | File sources: one thread, every frame is processed |
| `--backend` | `onnx` | `onnx` or `null` |
| `--model`, `--layout`, `--threads` | -, `yolox`, 0 | Model file, output layout, CPU threads (0 = automatic) |
| `--conf` | 0.25 | Confidence threshold |
| `--iou` | 0.45 | NMS overlap threshold |
| `--classes` | `sports ball,kite,frisbee` | Allowed COCO class names or class ids, or `all` |
| `--max-boxes` | 20 | Boxes per packet, best confidence first |
| `--dest` | `127.0.0.1:14601` | The companion's detection port (contract §9) |
| `--stats-s` | 5 | Period of the stats log line |
| `--view JPEG`, `--view-every N` | off, 10 | Bench viewer, see below |

The default classes are the COCO names that a party balloon is most often
reported as. This is a guess for the stand-in model (Provisional). For bench
tests with other objects use, for example, `--classes "person,cup"`. For a
single-class model use `--classes 0` or `--classes all`.

Class rule: a box gets the best of the *allowed* classes, not the best of
all 80. A balloon that scores `apple` 0.5 and `sports ball` 0.4 is sent as a
0.4 box. This gives the stand-in model more hits on a balloon. It also gives
more false boxes than a strict filter.

## What goes into a packet

- One packet per processed frame, also when the frame has no boxes ([P1]).
  The packet is built and encoded by `skyweave2.drone.packets`.
- Boxes are on the [C2] flight grid, 1920 x 1200, top-left origin. `(0, 0)`
  is the center of the top-left pixel, so the image spans -0.5 to 1919.5 and
  -0.5 to 1199.5. A detector reports edges where 0 is the left edge of the
  left pixel; percepd subtracts the half pixel.
- If the source is not 1920 x 1200, each axis is scaled to the grid. The
  whole source image maps to the whole grid. A source with another aspect
  ratio is stretched. This is for bench sources only.
- Boxes are clipped to the image. A box narrower or lower than 1 px after
  clipping is not sent.
- Packet pixels are raw sensor pixels. percepd does not undistort ([C2]).
- `frame_seq` counts grabbed frames from 0. A gap means frames that were
  grabbed and not processed.
- If the detector fails on a frame, percepd sends nothing for that frame.

## `t_cap`: what the stamp is

`t_cap` is board-monotonic milliseconds (`CLOCK_MONOTONIC`, the companion's
clock, [C1]). percepd reads the clock when the grab call returns, before the
color conversion.

The stamp is later than the true capture time. It ignores:

- the exposure time (the stamp is after the end of the exposure, not at its
  middle),
- the sensor readout and the USB transfer,
- the time the frame waits in the driver queue,
- the scheduling delay of the grab thread.

None of these is measured. Contract finding E1-F1 stays open. The grab thread
reads every frame when it arrives, so the queue wait stays small while that
thread keeps up.

A bench aid: for a camera, the stats line shows `driver_lag_ms`. This is the
stamp minus the driver's own buffer timestamp (OpenCV `CAP_PROP_POS_MSEC`).
For `uvcvideo` that timestamp should be `CLOCK_MONOTONIC` at the start of
the frame's USB transfer. This is not verified on the Cubie. If the value is
a small steady number, the driver timestamp is a better `t_cap` source.

For file sources `t_cap` is the read time, not the time the pictures were
taken.

## Keep-latest

A grab thread reads every frame and puts it in a one-frame slot. The
detector takes the newest frame. If the detector is slower than the camera,
older frames are replaced, not queued. The result is gaps in `frame_seq`,
not latency. `--every-frame` turns this off for file sources.

## Failures

percepd does not exit on these. It logs, counts, and goes on:

- The camera is missing or unplugged, or a grab fails: the source is closed
  and opened again. The wait starts at 0.5 s and doubles up to 5 s. A good
  frame resets it.
- The detector raises an error: that frame is skipped.
- A send fails: the packet is lost and counted.
- The viewer cannot write its file: counted.

SIGTERM and Ctrl-C stop the loop and the process exits with code 0. A bad
command line or a model that does not load exits with code 2 at start.

The stats line, every 5 s:

```
percepd fps_in=79.8 fps_out=11.9 infer_ms=81.7 infer_max_ms=96.0 boxes=24 drops=340 | total: grabbed=... sent=... drops=... grab_fail=0 opens=1 detect_err=0 send_err=0
```

`fps_in` is grabbed frames per second, `fps_out` is sent packets per second,
`infer_ms` is the mean time of one detector call in the interval, `drops` is
frames replaced in the slot. (The numbers in this example are made up.)

## Bench viewer

`--view /tmp/percepd/latest.jpg` writes the newest processed frame, with its
boxes, class names and confidences, to that one file every `--view-every`
frames. The file is replaced in one step. To look at it from another
computer, serve the folder (`python3 -m http.server` in `/tmp/percepd`) or
copy the file.

The viewer is off by default. It keeps one picture and is not a recording.
Do not turn it on in flight (project fence: no in-flight frame recording).

## Tests

```bash
v2/docker/sitl/run.sh pytest tests/drone/test_d_percepd.py -q
```

The tests need no camera, no model and no network. One more test runs a real
model when you point at one:

```bash
SKYWEAVE_PERCEPD_MODEL=_local/models/yolox_tiny.onnx \
SKYWEAVE_PERCEPD_IMAGE=/path/to/picture_with_a_ball.jpg \
SKYWEAVE_PERCEPD_CLASSES="sports ball" \
uv run pytest tests/drone/test_d_percepd.py -k real_onnx
```

## Later

- **The trained model.** Add one decoder function to `layouts.py` and one
  entry to `LAYOUTS` if its output layout is new.
- **The NPU backend.** A new class with `detect(frame) -> list[Detection]`
  beside `OnnxBackend`. It can use `make_blob` and `postprocess` as they are.
  It needs the vendor runtime, the compiled model file, the kernel preflight
  of brief work item 7 (refuse loudly off the Rabs9 kernel), and care with
  quantized outputs (scale and zero point before the decoder).
- **Not built yet** from brief work item 7: the systemd unit (B-D3 owns the
  service files), a config file, the ~1 Hz heartbeat, the cue hook.
