"""EXP-001 Tier 1 clean-scene generator (Blender/bpy, headless).

Renders linear radiance EXRs plus truth sidecars for the frozen scene in
``v2/configs/exp001_scene.yaml``. The renderer's job ends at radiance: the
frozen render settings below (Standard view transform, denoiser OFF,
motion blur ON with shutter = exposure/frame time, half-float linear EXR,
explicit per-frame seed, pinned version recorded) are the D3-brief table,
not preferences.

Blender's bundled Python has no PyYAML, so the frozen YAML is converted on
the host first; the YAML stays authoritative and the host embeds a
``_host`` block (sensor-model version, git revision) that this script
consumes and pops before any hashing:

    uv run python -m skyweave2.dataset to-json configs/exp001_scene.yaml /tmp/exp001.json
    blender -b -P blender/exp001_generate.py -- --scene-json /tmp/exp001.json \
        --out out/exp001 --benchmark

Modes:
    --benchmark          render ONE mid-clip frame on camera 0, time it, and
                         write benchmark.json with the x1350 full-clip figure
                         (render budget rule: no predictions, one timed frame)
    --frame N --camera I render one specific frame/camera
    (neither)            render the full clip (all cameras x all frames)

The warp margin is config (``--margin-px``, default 48) per the frozen
Blender-settings table; stage B consumes it via
``SensorModelSpec.warp_margin_px``.

Scene-construction choices not fixed by the manifest (all Provisional,
recorded into dataset.json): target altitude 20 m, cameras at z = 1.5 m
aimed at the mid-crossing point, sun elevation 35 deg / azimuth 140 deg,
Cycles 128 samples. The crossing is time-shifted so the first frame with
the target inside EVERY camera's nominal frame lands exactly at the
manifest's ``target_entry_s`` (warm-up stays background-only), and the
shared-FOV window is checked against ``shared_fov_min_s``.

Truth conventions: world is ENU (Blender Z-up maps directly); cameras.json
stores OpenCV-convention T_world_cam (+Z optical, +Y down) per the D0
contract, converted from Blender's -Z-forward/+Y-up camera axes. Motion
blur position is CENTER so the frame timestamp is the exposure midpoint
(D0 capture_ts decision). Rolling-shutter row time is not modeled in Tier 1;
the sidecar carries ``line_readout_us`` explicitly as 0.0 Provisional until
the bench measurement lands (design §4.2: the field must exist regardless).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from pathlib import Path

import bpy
import numpy as np

DEFAULT_MARGIN_PX = 48
CYCLES_SAMPLES = 128
TARGET_ALT_M = 20.0
CAMERA_Z_M = 1.5
SUN_ELEVATION_DEG = 35.0
SUN_AZIMUTH_DEG = 140.0
SKY_MODEL_FALLBACKS = ("MULTIPLE_SCATTERING", "NISHITA", "SINGLE_SCATTERING")
LINE_READOUT_US_PROVISIONAL = 0.0  # bench-measured later (screen-banding test)


def parse_args() -> argparse.Namespace:
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser(description="EXP-001 Tier 1 generator")
    parser.add_argument("--scene-json", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--frame", type=int, default=None)
    parser.add_argument("--camera", type=int, default=None)
    parser.add_argument("--benchmark", action="store_true")
    parser.add_argument("--samples", type=int, default=CYCLES_SAMPLES)
    parser.add_argument("--margin-px", type=int, default=DEFAULT_MARGIN_PX)
    parser.add_argument(
        "--device", choices=("CPU", "OPTIX", "CUDA"), default="CPU",
        help="Cycles compute device; OPTIX/CUDA enable the GPU (cloud L4 path)",
    )
    parser.add_argument(
        "--negative", action="store_true",
        help="target-free clip: same sky/cameras/timing, no target object; "
        "labels.jsonl is emitted empty and dataset.json records negative=true",
    )
    parser.add_argument(
        "--no-npy", action="store_true",
        help="skip the .npy companions (cloud renders: EXRs only, convert locally)",
    )
    parser.add_argument(
        "--skip-existing", action="store_true",
        help="skip frames whose EXR already exists (resume after spot preemption)",
    )
    return parser.parse_args(argv)


def enable_gpu(device: str) -> str:
    """Route Cycles to the GPU; returns a description of what was enabled."""
    if device == "CPU":
        bpy.context.scene.cycles.device = "CPU"
        return "CPU"
    prefs = bpy.context.preferences.addons["cycles"].preferences
    prefs.compute_device_type = device
    prefs.get_devices()
    names = []
    for dev in prefs.devices:
        dev.use = dev.type != "CPU"
        if dev.use:
            names.append(dev.name)
    if not names:
        raise RuntimeError(f"no {device} devices found; refusing to fall back silently")
    bpy.context.scene.cycles.device = "GPU"
    return f"{device}: {', '.join(sorted(set(names)))}"


def gpu_driver_version() -> str | None:
    import subprocess

    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=15,
        )
        return out.stdout.strip().splitlines()[0] if out.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, IndexError):
        return None


def dataset_id_inline(
    manifest: dict,
    blender_version: str,
    sensor_model_version: str,
    seed: int,
    git_rev: str,
    variant: str = "gate",
) -> str:
    """Mirror of skyweave2.dataset.dataset_id (bpy cannot import the package).

    The manifest passed here must already have the ``_host`` block removed.
    ``variant`` distinguishes the gate and negative clips of one manifest —
    without it the two datasets would share an id.
    """
    digest = hashlib.sha256()
    digest.update(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode())
    for part in (blender_version, sensor_model_version, str(seed), git_rev, variant):
        digest.update(b"\x00")
        digest.update(part.encode())
    return digest.hexdigest()


def look_at_matrix(position, target):
    """OpenCV-convention T_world_cam (4x4 row-major lists) and Blender world matrix."""
    import mathutils

    pos = mathutils.Vector(position)
    z_cam = (mathutils.Vector(target) - pos).normalized()  # optical axis
    up = mathutils.Vector((0.0, 0.0, 1.0))
    x_cam = z_cam.cross(up).normalized()
    y_cam = z_cam.cross(x_cam)  # points "down"
    t_cv = [
        [x_cam.x, y_cam.x, z_cam.x, pos.x],
        [x_cam.y, y_cam.y, z_cam.y, pos.y],
        [x_cam.z, y_cam.z, z_cam.z, pos.z],
        [0.0, 0.0, 0.0, 1.0],
    ]
    # Blender camera: -Z forward, +Y up  =>  R_blender = R_cv @ diag(1,-1,-1).
    m = mathutils.Matrix(
        (
            (x_cam.x, -y_cam.x, -z_cam.x, pos.x),
            (x_cam.y, -y_cam.y, -z_cam.y, pos.y),
            (x_cam.z, -y_cam.z, -z_cam.z, pos.z),
            (0.0, 0.0, 0.0, 1.0),
        )
    )
    return t_cv, m


def project_truth(record: dict, position) -> dict:
    """Project the target center with the RENDER-grid pinhole (no distortion)."""
    t = record["t_world_cam"]
    r = [[t[i][j] for j in range(3)] for i in range(3)]
    c = [t[i][3] for i in range(3)]
    d = [position[i] - c[i] for i in range(3)]
    p_cam = [sum(r[k][i] * d[k] for k in range(3)) for i in range(3)]  # R^T @ (p - c)
    if p_cam[2] <= 1e-9:
        return {"visible": False}
    x, y = p_cam[0] / p_cam[2], p_cam[1] / p_cam[2]
    u_nominal = record["fx"] * x + record["cx"]
    v_nominal = record["fy"] * y + record["cy"]
    return {
        "visible": bool(
            -0.5 <= u_nominal <= record["nominal_width"] - 0.5
            and -0.5 <= v_nominal <= record["nominal_height"] - 0.5
        ),
        "u_nominal": u_nominal,
        "v_nominal": v_nominal,
        "u_render": u_nominal + record["margin_px"],
        "v_render": v_nominal + record["margin_px"],
    }


def _select_sky_model(sky, manifest: dict) -> str:
    """Honor the manifest's pinned sky model first, then the fallback chain."""
    pinned = manifest.get("determinism", {}).get("sky_model")
    candidates = ((pinned,) if pinned else ()) + SKY_MODEL_FALLBACKS
    for sky_type in candidates:
        try:
            sky.sky_type = sky_type
            return sky.sky_type
        except TypeError:
            continue
    raise RuntimeError(f"no supported sky model in this Blender; tried {candidates}")


def build_scene(manifest: dict, samples: int, margin_px: int, negative: bool = False):
    scene = bpy.context.scene
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)

    cams = manifest["cameras"]
    timing = manifest["timing"]
    target_spec = manifest["target"]
    seed = int(manifest["determinism"]["seed"])
    width, height = cams["render_resolution"]
    fps = float(timing["fps"])
    n_frames = int(round(fps * float(timing["duration_s"])))

    # Render/engine settings (frozen table).
    scene.render.engine = "CYCLES"
    scene.cycles.samples = samples
    scene.cycles.use_denoising = False
    scene.cycles.use_animated_seed = False
    scene.view_settings.view_transform = "Standard"
    scene.view_settings.look = "None"
    scene.render.fps = int(fps)
    scene.frame_start = 0
    scene.frame_end = n_frames - 1

    exposure_s = float(timing["exposure_us"]) * 1e-6
    scene.render.use_motion_blur = True
    scene.render.motion_blur_shutter = exposure_s * fps  # fraction of frame time
    scene.render.motion_blur_position = "CENTER"  # frame time == exposure midpoint

    # Pinhole cameras with warp margin: widen FOV so the nominal frame sits
    # centered inside the rendered frame with margin_px on each side.
    render_w, render_h = width + 2 * margin_px, height + 2 * margin_px
    scene.render.resolution_x = render_w
    scene.render.resolution_y = render_h
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "OPEN_EXR"
    scene.render.image_settings.color_mode = "RGB"
    scene.render.image_settings.color_depth = "16"
    scene.render.image_settings.exr_codec = "ZIP"

    fov_h = math.radians(float(cams["fov_h_deg"]))
    fov_ext = 2.0 * math.atan(
        math.tan(fov_h / 2.0) * (width / 2.0 + margin_px) / (width / 2.0)
    )
    fx = (width / 2.0) / math.tan(fov_h / 2.0)

    # Sky.
    world = bpy.data.worlds.new("exp001_world")
    scene.world = world
    world.use_nodes = True
    nodes = world.node_tree.nodes
    nodes.clear()
    sky = nodes.new("ShaderNodeTexSky")
    sky_model = _select_sky_model(sky, manifest)
    if hasattr(sky, "sun_elevation"):
        sky.sun_elevation = math.radians(SUN_ELEVATION_DEG)
    if hasattr(sky, "sun_rotation"):
        sky.sun_rotation = math.radians(SUN_AZIMUTH_DEG)
    background = nodes.new("ShaderNodeBackground")
    output = nodes.new("ShaderNodeOutputWorld")
    world.node_tree.links.new(sky.outputs["Color"], background.inputs["Color"])
    world.node_tree.links.new(background.outputs["Background"], output.inputs["Surface"])

    # Cameras along the east axis, aimed at the mid-crossing point.
    range_m = float(target_spec["nominal_range_m"])
    aim = (0.0, range_m, TARGET_ALT_M)
    baseline = float(cams["baseline_m"])
    count = int(cams["count"])
    xs = [(-baseline / 2.0) + baseline * i / (count - 1) for i in range(count)]
    camera_records = []
    camera_objects = []
    for i, x in enumerate(xs):
        cam_data = bpy.data.cameras.new(f"cam{i}")
        cam_data.type = "PERSP"
        cam_data.sensor_fit = "HORIZONTAL"
        cam_data.angle = fov_ext
        cam_data.clip_end = 10000.0
        cam_obj = bpy.data.objects.new(f"cam{i}", cam_data)
        scene.collection.objects.link(cam_obj)
        t_cv, matrix = look_at_matrix((x, 0.0, CAMERA_Z_M), aim)
        cam_obj.matrix_world = matrix
        camera_objects.append(cam_obj)
        camera_records.append(
            {
                "camera_id": i,
                "t_world_cam": t_cv,
                "nominal_width": width,
                "nominal_height": height,
                "render_width": render_w,
                "render_height": render_h,
                "margin_px": margin_px,
                "fx": fx,
                "fy": fx,
                "cx": (width - 1) / 2.0,
                "cy": (height - 1) / 2.0,
                "fov_h_deg_nominal": float(cams["fov_h_deg"]),
                "fov_h_deg_rendered": math.degrees(fov_ext),
            }
        )

    if negative:
        # Target-free clip: identical sky, cameras, timing — no target, no
        # trajectory, no entry alignment. Used for the false-proposal rate.
        return {
            "seed": seed,
            "fps": fps,
            "n_frames": n_frames,
            "exposure_s": exposure_s,
            "target_pos": None,
            "camera_objects": camera_objects,
            "camera_records": camera_records,
            "target_obj": None,
            "sky_model": sky_model,
            "margin_px": margin_px,
            "entry_s": None,
            "shared_window_s": None,
            "negative": True,
        }

    # Target trajectory: lateral constant velocity, time-shifted so the first
    # shared-FOV frame lands exactly at target_entry_s (manifest warm-up is
    # background-only by definition; trajectory shifts in time linearly).
    speed = float(target_spec["speed_mps"])
    duration = float(timing["duration_s"])
    entry_goal = float(timing.get("target_entry_s", 0.0))
    mid_t = duration / 2.0

    def pos_at(t: float, mid: float):
        return (speed * (t - mid), range_m, TARGET_ALT_M)

    def shared_visible(t: float, mid: float) -> bool:
        p = pos_at(t, mid)
        return all(project_truth(rec, p)["visible"] for rec in camera_records)

    step = 1e-3
    t_entry = next(
        (t * step for t in range(int(duration / step)) if shared_visible(t * step, mid_t)),
        None,
    )
    if t_entry is None:
        raise RuntimeError("target never enters the shared FOV; scene geometry is wrong")
    mid_t += entry_goal - t_entry  # linear shift: new entry time == entry_goal
    t_exit = next(
        (
            t * step
            for t in range(int(entry_goal / step), int(duration / step))
            if not shared_visible(t * step, mid_t)
        ),
        duration,
    )
    shared_window_s = t_exit - entry_goal
    min_window = float(timing.get("shared_fov_min_s", 0.0))
    if shared_window_s < min_window:
        raise RuntimeError(
            f"shared-FOV window {shared_window_s:.2f}s < manifest minimum {min_window}s"
        )

    bpy.ops.mesh.primitive_uv_sphere_add(radius=float(target_spec["width_m"]) / 2.0)
    target_obj = bpy.context.active_object
    target_obj.name = "exp001_target"
    material = bpy.data.materials.new("target_matte")
    material.use_nodes = True
    bsdf = material.node_tree.nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = (0.03, 0.03, 0.03, 1.0)
    bsdf.inputs["Roughness"].default_value = 0.9
    target_obj.data.materials.append(material)

    # Linear interpolation between the two keys = exact constant velocity
    # (the manifest freezes lateral_constant_velocity; Bezier ease violates
    # the scene and desynchronizes truth from pixels by tens of pixels).
    # Blender 5.x slotted actions IGNORE the new-keyframe interpolation
    # preference — discovered when the first cloud render drifted ~90 px off
    # truth away from mid-clip — so LINEAR is forced explicitly on every
    # keyframe through whichever fcurve API this Blender exposes.
    bpy.context.preferences.edit.keyframe_new_interpolation_type = "LINEAR"
    for frame in (0, n_frames - 1):
        target_obj.location = pos_at(frame / fps, mid_t)
        target_obj.keyframe_insert(data_path="location", frame=frame)
    _force_linear_interpolation(target_obj)
    _assert_linear_motion(target_obj, pos_at, mid_t, fps, n_frames)

    return {
        "seed": seed,
        "fps": fps,
        "n_frames": n_frames,
        "exposure_s": exposure_s,
        "target_pos": lambda t: pos_at(t, mid_t),
        "camera_objects": camera_objects,
        "camera_records": camera_records,
        "target_obj": target_obj,
        "sky_model": sky_model,
        "margin_px": margin_px,
        "entry_s": entry_goal,
        "shared_window_s": shared_window_s,
        "negative": False,
    }


def _force_linear_interpolation(obj) -> None:
    """Set every keyframe on the object's action to LINEAR, on both the
    legacy (Action.fcurves) and the Blender 5 layered/slotted API."""
    action = obj.animation_data.action
    fcurves = []
    if getattr(action, "fcurves", None):
        fcurves.extend(action.fcurves)
    for layer in getattr(action, "layers", []):
        for strip in layer.strips:
            for bag in strip.channelbags:
                fcurves.extend(bag.fcurves)
    if not fcurves:
        raise RuntimeError("no fcurves found on the target action; keyframing failed")
    for fc in fcurves:
        for kp in fc.keyframe_points:
            kp.interpolation = "LINEAR"


def _assert_linear_motion(obj, pos_at, mid_t: float, fps: float, n_frames: int) -> None:
    """Hard gate before any render: evaluated position must match the
    constant-velocity truth at off-midpoint frames (midpoint alone cannot
    catch interpolation bugs — learned the expensive way)."""
    scene = bpy.context.scene
    for frame in (n_frames // 4, n_frames // 2, (3 * n_frames) // 4):
        scene.frame_set(frame)
        actual = obj.matrix_world.translation
        expected = pos_at(frame / fps, mid_t)
        error = max(abs(actual[i] - expected[i]) for i in range(3))
        if error > 1e-3:
            raise RuntimeError(
                f"trajectory interpolation broken: frame {frame} evaluated "
                f"{tuple(actual)} vs constant-velocity {expected} (err {error:.3f} m)"
            )
    scene.frame_set(0)


def render_one(
    scene_info,
    camera_index: int,
    frame: int,
    out_dir: Path,
    seed: int,
    write_npy: bool = True,
    skip_existing: bool = False,
) -> Path:
    import os

    scene = bpy.context.scene
    out_path = out_dir / f"cam{camera_index}" / f"radiance_{frame:06d}.exr"
    if skip_existing and out_path.exists():
        return out_path
    scene.camera = scene_info["camera_objects"][camera_index]
    scene.frame_set(frame)
    scene.cycles.seed = seed * 100000 + frame  # explicit per-frame seed rule
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Render to a temp name and atomically rename: --skip-existing may then
    # trust bare existence, because a preemption mid-write leaves only the
    # temp file, never a truncated file under the final name.
    tmp_path = out_path.with_name(f".tmp_{out_path.name}")
    scene.render.filepath = str(tmp_path)
    bpy.ops.render.render(write_still=True)
    os.replace(tmp_path, out_path)

    if write_npy:
        # Companion .npy so host-side checks need no EXR reader.
        image = bpy.data.images.load(str(out_path))
        try:
            pixels = np.array(image.pixels[:], dtype=np.float32)
            w, h = image.size
            rgb = pixels.reshape(h, w, image.channels)[::-1, :, :3]  # rows are bottom-up
            tmp_npy = out_path.with_name(f".tmp_{out_path.stem}.npy")
            np.save(tmp_npy, rgb.astype(np.float16))
            os.replace(tmp_npy, out_path.with_suffix(".npy"))
        finally:
            bpy.data.images.remove(image)
    return out_path


def write_sidecars(
    scene_info,
    manifest: dict,
    host: dict,
    out_dir: Path,
    benchmark: dict | None,
    render_device: str = "CPU",
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    truth_dir = out_dir / "truth"
    truth_dir.mkdir(exist_ok=True)
    (truth_dir / "cameras.json").write_text(
        json.dumps(scene_info["camera_records"], indent=2, sort_keys=True) + "\n"
    )
    fps, n_frames = scene_info["fps"], scene_info["n_frames"]
    exposure_s = scene_info["exposure_s"]
    trajectory = open(truth_dir / "trajectory.jsonl", "w", encoding="utf-8", newline="\n")
    labels = open(truth_dir / "labels.jsonl", "w", encoding="utf-8", newline="\n")
    with trajectory, labels:
        for frame in range(n_frames if not scene_info["negative"] else 0):
            t_mid = frame / fps  # motion_blur_position=CENTER: frame time = midpoint
            pos = scene_info["target_pos"](t_mid)
            projections = {
                str(rec["camera_id"]): project_truth(rec, pos)
                for rec in scene_info["camera_records"]
            }
            trajectory.write(
                json.dumps(
                    {
                        "frame_seq": frame,
                        "t_mid_s": t_mid,
                        "exposure_start_s": t_mid - exposure_s / 2.0,
                        "exposure_end_s": t_mid + exposure_s / 2.0,
                        "line_readout_us": LINE_READOUT_US_PROVISIONAL,
                        "target_world_m": list(pos),
                        "projections": projections,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
            # One label row per camera per frame (schema: skyweave2.eval.labels;
            # confusers will append here with their own object ids in Tier 1.5).
            for rec in scene_info["camera_records"]:
                proj = projections[str(rec["camera_id"])]
                labels.write(
                    json.dumps(
                        {
                            "frame_seq": frame,
                            "camera_id": rec["camera_id"],
                            "object_id": 0,
                            "object_class": "target",
                            "u": proj.get("u_nominal", -1.0),
                            "v": proj.get("v_nominal", -1.0),
                            "visible": proj["visible"],
                            "size_px": None,
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )

    pinned_version = manifest.get("determinism", {}).get("renderer_version")
    version_matches = pinned_version is None or bpy.app.version_string.startswith(
        pinned_version.split(" ")[0]
    )
    if not version_matches:
        print(
            f"WARNING: running Blender {bpy.app.version_string} but manifest pins "
            f"{pinned_version}; dataset_id will differ from the pinned lineage"
        )
    dataset = {
        "schema": "exp001-dataset/1",
        "negative": scene_info["negative"],
        "render_device": render_device,
        "gpu_driver_version": gpu_driver_version(),
        "blender_version": bpy.app.version_string,
        "blender_version_matches_manifest_pin": version_matches,
        "sensor_model_version": host.get("sensor_model_version", "unknown"),
        "git_rev": host.get("git_rev", "no-git"),
        "seed": scene_info["seed"],
        "dataset_id": dataset_id_inline(
            manifest,
            bpy.app.version_string,
            host.get("sensor_model_version", "unknown"),
            scene_info["seed"],
            host.get("git_rev", "no-git"),
            variant="negative" if scene_info["negative"] else "gate",
        ),
        "line_readout_us_label": "Provisional 0.0 until bench-measured (D3 bench tasks)",
        "provisional_scene_choices": {
            "target_alt_m": TARGET_ALT_M,
            "camera_z_m": CAMERA_Z_M,
            "sun_elevation_deg": SUN_ELEVATION_DEG,
            "sun_azimuth_deg": SUN_AZIMUTH_DEG,
            "sky_model": scene_info["sky_model"],
            "margin_px": scene_info["margin_px"],
            "cycles_samples": bpy.context.scene.cycles.samples,
            "shared_fov_entry_s": scene_info["entry_s"],
            "shared_fov_window_s": scene_info["shared_window_s"],
        },
    }
    if benchmark:
        dataset["benchmark"] = benchmark
        (out_dir / "benchmark.json").write_text(
            json.dumps(benchmark, indent=2, sort_keys=True) + "\n"
        )
    (out_dir / "dataset.json").write_text(json.dumps(dataset, indent=2, sort_keys=True) + "\n")


def main() -> None:
    args = parse_args()
    manifest = json.loads(Path(args.scene_json).read_text())
    host = manifest.pop("_host", {})  # host facts; never part of the hashed manifest
    out_dir = Path(args.out)
    scene_info = build_scene(manifest, args.samples, args.margin_px, negative=args.negative)
    seed = scene_info["seed"]

    # Resume guard: --skip-existing must never silently mix frames rendered
    # under different configurations into one dataset.
    fingerprint = {
        "samples": args.samples,
        "margin_px": args.margin_px,
        "negative": args.negative,
        "seed": seed,
        "manifest": json.dumps(manifest, sort_keys=True),
    }
    fp_path = out_dir / "render_config.json"
    if args.skip_existing and fp_path.exists():
        existing = json.loads(fp_path.read_text())
        if existing != fingerprint:
            raise RuntimeError(
                f"resume refused: {fp_path} was written by a different render "
                "configuration; use a fresh --out directory"
            )
    out_dir.mkdir(parents=True, exist_ok=True)
    fp_path.write_text(json.dumps(fingerprint, indent=2, sort_keys=True) + "\n")

    device_desc = enable_gpu(args.device)
    write_npy = not args.no_npy

    benchmark = None
    if args.benchmark:
        frame = scene_info["n_frames"] // 2  # mid-clip: target near frame center
        start = time.perf_counter()
        path = render_one(scene_info, 0, frame, out_dir, seed, write_npy=write_npy)
        per_frame_s = time.perf_counter() - start
        total_frames = scene_info["n_frames"] * len(scene_info["camera_objects"])
        benchmark = {
            "frame": frame,
            "camera": 0,
            "samples": args.samples,
            "per_frame_s": per_frame_s,
            "total_frames": total_frames,
            "full_clip_hours_at_this_rate": per_frame_s * total_frames / 3600.0,
            "device": bpy.context.scene.cycles.device,
            "blender_version": bpy.app.version_string,
            "exr": str(path),
        }
        print(
            f"BENCHMARK per_frame_s={per_frame_s:.2f} "
            f"full_clip_hours={benchmark['full_clip_hours_at_this_rate']:.2f} "
            f"({total_frames} frames)"
        )
    elif args.frame is not None:
        cams = (
            [args.camera]
            if args.camera is not None
            else range(len(scene_info["camera_objects"]))
        )
        for cam in cams:
            render_one(scene_info, cam, args.frame, out_dir, seed, write_npy=write_npy)
    else:
        clip_start = time.perf_counter()
        for frame in range(scene_info["n_frames"]):
            for cam in range(len(scene_info["camera_objects"])):
                render_one(
                    scene_info, cam, frame, out_dir, seed,
                    write_npy=write_npy, skip_existing=args.skip_existing,
                )
            if frame % 25 == 0:
                elapsed = time.perf_counter() - clip_start
                print(f"PROGRESS frame={frame}/{scene_info['n_frames']} elapsed_s={elapsed:.0f}",
                      flush=True)

    write_sidecars(scene_info, manifest, host, out_dir, benchmark, render_device=device_desc)
    print(f"wrote sidecars to {out_dir} (device: {device_desc})")


if __name__ == "__main__":
    main()
