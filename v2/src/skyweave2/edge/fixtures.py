"""Golden frame -> packet fixtures for D8.

The brief's scope item 4: known Y clips through the host ``ive_approx``
oracle produce expected observations and expected packet bytes. The fixture
policy is SPLIT and the split is the whole point:

- **Wire: exact.** For identical observation inputs the daemon's nanopb
  encoding must be byte-identical to the host codec. Absolute gate, no
  tolerance, tested by E2 against ``packets.hex``.
- **Detector: toleranced.** Hardware GMM2 and the daemon's portable GMM2 are
  compared to the ``ive_approx`` oracle within bounds DECLARED IN THE REPORT
  BEFORE the first board run. Divergence is a Measured result, not a
  failure, unless it breaks those bounds.

Three clips, for three different reasons:

``sparse``   four movers against a cap of seven, so the cap NEVER bites. This
             is the clip the declared detector tolerances are measured on:
             with the cap inert, a difference between two detectors is a
             difference in what they SAW, not in which seven of ten survived
             a ranking. Built here from a declared seed.
``clutter``  the same scene with ten movers, so the cap bites on almost every
             frame. Its job is the cap, the drop counters and the health
             path — and, as finding D8-F3 records, the discovery that the cap
             AMPLIFIES a small detector divergence into a different SUBSET of
             measurements. Its tolerance is declared separately and wider on
             exactly those axes, and tight on centroid error, so the two
             effects stay distinguishable.
``gate``     the real golden gate clip's Y planes (``output/exp001_clips/``),
             which are machine-local render artifacts. The DERIVED fixture
             (expected observations, expected packet bytes) is committed, so
             the byte gate runs everywhere; regenerating it needs the clip.

What is COMMITTED per fixture is the derived, small part: the detector
config, the observations (binary + JSONL), the expected packet bytes and a
stats/provenance file. Pixels are not. That split is what makes the wire
gate absolute: E2 feeds both encoders the committed ``observations.swob``,
so a new OpenCV or a new numpy can change what the detector FINDS without
being able to touch what the encoders PRODUCE. The detector's own drift
against those committed observations is the toleranced half (E5), which is
where it belongs.

Anti-tuning: the fixture configs change ``proc_width``/``proc_height``,
``warmup_frames`` and the backend selection and NOTHING else. Every detector
threshold is the shipped ``DetectorConfig`` default. The clutter scene is
constructed to be crowded, never to make a threshold look good.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from skyweave2.contracts import ClockDomain, FrameEnvelope, Observation2D
from skyweave2.detector.cap import DetectorStats
from skyweave2.detector.config import Backend, DetectorConfig
from skyweave2.detector.runner import detect_clip
from skyweave2.edge.injection import PtsProfile, build_injection_stream
from skyweave2.edge.obsfixture import CaptureEvent, encode_fixture, group_by_capture_event
from skyweave2.eval.clips import write_clip
from skyweave2.transport import codec
from skyweave2.transport.wire import WIRE_LIMITS

V2_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_ROOT = V2_ROOT / "tests" / "edge" / "fixtures"
GATE_CLIPS = V2_ROOT.parent / "output" / "exp001_clips" / "gate"

# --------------------------------------------------------------------------
# The clutter scene. Every number here is part of the fixture's identity and
# is recorded in `scene.json` beside the generated clip.
# --------------------------------------------------------------------------

SCENE_SEED = 7
# The clip is RENDERED at full resolution and DETECTED at half, so the D0
# scaling law runs with scale = 2 on both sides. A fixture where proc == full
# would let a daemon that forgot the law pass every test in the suite.
SCENE_FULL_WIDTH = 640
SCENE_FULL_HEIGHT = 480
SCENE_PROC_WIDTH = 320
SCENE_PROC_HEIGHT = 240
SCENE_FRAMES = 60
SCENE_WARMUP = 20
SCENE_FPS = 30.0
SCENE_BACKGROUND_DN = 96.0
SCENE_NOISE_DN = 2.0
SCENE_BLOB_AMPLITUDE_DN = 90.0
# Sizes and speeds are quoted at FULL resolution; after the 2x downscale they
# land at 1.7 px sigma and 1.5 px/frame on the detection grid.
SCENE_BLOB_SIGMA_PX = 3.4
SCENE_SPEED_PX_PER_FRAME = 3.0

# Four movers: comfortably under the cap of seven on every frame, so the cap
# is INERT and a detector difference is a detection difference.
SPARSE_BLOBS = 4
# Ten movers: over the cap by enough that a single missed blob cannot quietly
# stop the fixture from testing the cap at all.
CLUTTER_BLOBS = 10


def scene_config(name: str) -> DetectorConfig:
    """The oracle config for a synthetic fixture: defaults plus geometry.

    ``ive_approx`` because the brief names it as the D8 oracle — it is the
    backend whose knobs mirror RK_MPI_IVE_GMM2, so it is the one whose
    divergence from the board is the number worth measuring. Nothing else is
    touched: every threshold is the shipped default (anti-tuning).
    """
    return DetectorConfig(
        backend=Backend.IVE_APPROX,
        proc_width=SCENE_PROC_WIDTH,
        proc_height=SCENE_PROC_HEIGHT,
        warmup_frames=SCENE_WARMUP,
        fps=SCENE_FPS,
        detector_rev=f"d8-{name}/1",
        calibration_rev="d8-fixture-cal",
    )


def sparse_config() -> DetectorConfig:
    return scene_config("sparse")


def clutter_config() -> DetectorConfig:
    return scene_config("clutter")


def gate_config() -> DetectorConfig:
    """The oracle config for the gate fixture: the D4 primary resolution."""
    return DetectorConfig(
        backend=Backend.IVE_APPROX,
        proc_width=1536,
        proc_height=864,
        detector_rev="d8-gate/1",
        calibration_rev="d8-fixture-cal",
    )


def _scene_tracks(seed: int, blobs: int) -> list[tuple[np.ndarray, np.ndarray]]:
    """(start, velocity) per blob, seeded and independent of frame order."""
    rng = np.random.default_rng(np.random.SeedSequence([seed, 0xC1077E]))
    tracks = []
    for _ in range(blobs):
        start = rng.uniform((24.0, 24.0), (SCENE_FULL_WIDTH - 24.0, SCENE_FULL_HEIGHT - 24.0))
        angle = rng.uniform(0.0, 2.0 * np.pi)
        velocity = SCENE_SPEED_PX_PER_FRAME * np.array([np.cos(angle), np.sin(angle)])
        tracks.append((start, velocity))
    return tracks


def build_scene_clip(out_dir: str | Path, blobs: int, seed: int = SCENE_SEED) -> Path:
    """Write a synthetic mover clip. Byte-identical for a given (blobs, seed)."""
    out_dir = Path(out_dir)
    tracks = _scene_tracks(seed, blobs)
    v_grid, u_grid = np.meshgrid(
        np.arange(SCENE_FULL_HEIGHT, dtype=np.float64),
        np.arange(SCENE_FULL_WIDTH, dtype=np.float64),
        indexing="ij",
    )
    frames = []
    for frame_seq in range(SCENE_FRAMES):
        # Noise keyed by frame, not drawn from a running stream: a partial
        # regeneration must produce the same pixels as a full one.
        rng = np.random.default_rng(np.random.SeedSequence([seed, frame_seq, 0x0A15E]))
        image = SCENE_BACKGROUND_DN + rng.normal(
            0.0, SCENE_NOISE_DN, size=(SCENE_FULL_HEIGHT, SCENE_FULL_WIDTH)
        )
        # The blobs appear only AFTER warm-up. During warm-up the background
        # model must see the background alone, exactly as it would on a node
        # that started before anything flew.
        if frame_seq >= SCENE_WARMUP:
            moved = frame_seq - SCENE_WARMUP
            for start, velocity in tracks:
                cu, cv = start + velocity * moved
                if not (0 <= cu < SCENE_FULL_WIDTH and 0 <= cv < SCENE_FULL_HEIGHT):
                    continue
                image += SCENE_BLOB_AMPLITUDE_DN * np.exp(
                    -0.5
                    * (
                        ((u_grid - cu) / SCENE_BLOB_SIGMA_PX) ** 2
                        + ((v_grid - cv) / SCENE_BLOB_SIGMA_PX) ** 2
                    )
                )
        frames.append(np.clip(np.rint(image), 0, 255).astype(np.uint8))
    write_clip(
        frames,
        out_dir,
        fps=SCENE_FPS,
        source=f"d8-scene seed {seed} ({blobs} movers)",
    )
    (out_dir / "scene.json").write_text(
        json.dumps(
            {
                "schema": "d8-scene/1",
                "seed": seed,
                "blobs": blobs,
                "frames": SCENE_FRAMES,
                "warmup_frames": SCENE_WARMUP,
                "full_width": SCENE_FULL_WIDTH,
                "full_height": SCENE_FULL_HEIGHT,
                "proc_width": SCENE_PROC_WIDTH,
                "proc_height": SCENE_PROC_HEIGHT,
                "background_dn": SCENE_BACKGROUND_DN,
                "noise_dn": SCENE_NOISE_DN,
                "blob_amplitude_dn": SCENE_BLOB_AMPLITUDE_DN,
                "blob_sigma_px": SCENE_BLOB_SIGMA_PX,
                "speed_px_per_frame": SCENE_SPEED_PX_PER_FRAME,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return out_dir


# --------------------------------------------------------------------------
# Fixture assembly
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FixtureBuild:
    """One fixture's derived artifacts, in memory."""

    name: str
    config: DetectorConfig
    session_uuid: str
    camera_id: int
    observations: list[Observation2D]
    stats: DetectorStats
    packets: list[bytes]
    injection_sha256: str
    # The seed that produced this fixture's PIXELS, or None when no seed did.
    # The gate fixture's clip is a render artifact, so recording a seed for it
    # would assert a determinism input it does not have — the standing rule is
    # that the artifact records the seed it was built with, and "none" is an
    # honest answer where "7" is a false one.
    seed: int | None = SCENE_SEED


def _environment() -> dict[str, str]:
    """What produced these bytes. Recorded, never asserted on.

    The committed observations are a DETECTOR output, so they carry the
    detector's dependencies with them. Nothing gates on these strings — a
    version gate would fail on machines the fixture is fine on — but when a
    regeneration moves a centroid, this is the first place to look.
    """
    import cv2
    import google.protobuf

    return {
        "python": platform.python_version(),
        "numpy": np.__version__,
        "opencv": cv2.__version__,
        "protobuf": google.protobuf.__version__,
    }


def _session_uuid(name: str) -> str:
    """A declared, stable session id per fixture.

    Not derived from the clip and config hash the way ``runner.detect_clip``
    derives its own: the fixture's identity must survive an unrelated
    ``DetectorConfig`` field being added, or every committed packet byte
    would churn on a change that touched no encoding.
    """
    return f"d8fixture-{name}-0000-0000-000000000001"[:36]


def build_fixture(
    name: str,
    clip_dir: str | Path,
    config: DetectorConfig,
    camera_id: int = 0,
    profile: PtsProfile | None = None,
    seed: int | None = SCENE_SEED,
) -> FixtureBuild:
    """Run the oracle over a clip and encode every capture event."""
    stats = DetectorStats()
    observations, _ = detect_clip(clip_dir, config, camera_id=camera_id, stats=stats)
    session = _session_uuid(name)
    observations = [
        obs.model_copy(
            update={"envelope": obs.envelope.model_copy(update={"session_uuid": session})}
        )
        for obs in observations
    ]
    packets = [
        codec.encode_observation_packet(envelope, event)
        for envelope, event in group_by_capture_event(observations)
    ]
    # The injection stream is REGENERATED, never committed (it is the pixels
    # again, plus a header). Its digest is committed instead, so E3's
    # determinism claim is pinned across machines and not just within a run.
    injection = build_injection_stream(
        clip_dir, config, session, camera_id=camera_id, profile=profile
    )
    return FixtureBuild(
        name=name,
        config=config,
        session_uuid=session,
        camera_id=camera_id,
        observations=observations,
        stats=stats,
        packets=packets,
        injection_sha256=hashlib.sha256(injection).hexdigest(),
        seed=seed,
    )


def write_fixture(build: FixtureBuild, out_dir: str | Path) -> Path:
    """Commit one fixture: config, observations (binary + JSONL), packets, stats."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    events = group_by_capture_event(build.observations)

    (out_dir / "config.json").write_text(
        json.dumps(build.config.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (out_dir / "observations.swob").write_bytes(encode_fixture(events))
    with open(out_dir / "observations.jsonl", "w", encoding="utf-8", newline="\n") as fh:
        for obs in build.observations:
            fh.write(json.dumps(obs.model_dump(mode="json"), sort_keys=True) + "\n")
    with open(out_dir / "packets.hex", "w", encoding="utf-8", newline="\n") as fh:
        for packet in build.packets:
            fh.write(packet.hex() + "\n")
    (out_dir / "stats.json").write_text(
        json.dumps(
            {
                "name": build.name,
                "seed": build.seed,
                "session_uuid": build.session_uuid,
                "camera_id": build.camera_id,
                "capture_events": len(events),
                "observations": len(build.observations),
                "max_observations_per_event": max(
                    (len(event) for _, event in events), default=0
                ),
                "packet_bytes_max": max((len(p) for p in build.packets), default=0),
                "detector": build.stats.as_dict(),
                "injection_sha256": build.injection_sha256,
                "environment": _environment(),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return out_dir


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# The wire-cases fixture: hand-built, adversarial, no clip at all
# --------------------------------------------------------------------------


def wire_case_events() -> list[CaptureEvent]:
    """Capture events chosen to break a plausible-but-wrong C encoder.

    A fixture harvested from a clip is a poor byte gate: every observation in
    it looks the same. Real clips do not produce a bbox at the origin, an
    absent optional, a negative capture timestamp or a string at its declared
    bound — and each of those is a distinct way for two encoders to disagree
    while both look correct:

    - an ALL-ZERO bbox is an empty PRESENT submessage; an encoder that skips
      "empty" submessages drops field 8 entirely and every other case still
      passes;
    - ``local_blob_id`` absent vs 0 and ``evidence_ref`` absent vs "" are the
      explicit-presence pair; conflating them costs two bytes each way;
    - a NEGATIVE ``capture_ts_ns`` costs a full 10-byte varint because proto3
      sign-extends int64, and a bbox origin off the left edge exercises the
      zigzag path;
    - strings at their declared ``max_size`` are where an off-by-one in the
      NUL accounting shows up;
    - values that are not binary32-representable pin the narrowing to the
      same five D0-declared ``float`` fields on both sides;
    - a FULL event at the declared bound exercises the repeated field's
      length prefix crossing a varint boundary.
    """
    limits = WIRE_LIMITS
    text = limits.text_len

    def envelope(**overrides) -> FrameEnvelope:
        values = dict(
            camera_id=2,
            session_uuid="8f14e45f-ceea-467a-9d3f-1b2c3d4e5f60",
            frame_seq=1234,
            capture_ts_ns=41_133_333_333,
            clock_domain=ClockDomain.SYNTHETIC,
            time_sync_error_ms=0.5,
            exposure_us=3000.0,
            gain_db=6.0,
            full_width=2304,
            full_height=1296,
            proc_width=1536,
            proc_height=864,
            calibration_rev="d8-fixed-cal-r1",
            detector_rev="d8-ive-1536",
            line_readout_us=0.0,
        )
        values.update(overrides)
        return FrameEnvelope(**values)

    def observation(env: FrameEnvelope, **overrides) -> Observation2D:
        values = dict(
            envelope=env,
            obs_id=1,
            u=1152.3125,
            v=647.75,
            cov_uu=0.25,
            cov_uv=0.0,
            cov_vv=0.25,
            bbox_x=1149,
            bbox_y=644,
            bbox_w=6,
            bbox_h=6,
            area_px=12,
            persistence_count=2,
            confidence=1.0,
            local_blob_id=3,
            evidence_ref="patch-0001",
        )
        values.update(overrides)
        return Observation2D(**values)

    events: list[CaptureEvent] = []

    # 1. The plain case, identical in shape to D7's `observation_single`.
    plain = envelope()
    events.append((plain, [observation(plain)]))

    # 2. Presence: absent vs zero vs empty, in one event.
    presence = envelope(frame_seq=1235)
    events.append(
        (
            presence,
            [
                observation(presence, obs_id=0, local_blob_id=None, evidence_ref=None),
                observation(presence, obs_id=1, local_blob_id=0, evidence_ref=""),
            ],
        )
    )

    # 2b. The smallest observation the CONTRACT permits: every field the
    #     contract lets sit at proto3's implicit default does, so those
    #     fields vanish from the encoding entirely and only the ones D0
    #     forbids from being zero survive. `bbox_x`/`bbox_y` at 0 make the
    #     bbox a PRESENT submessage with two of its four fields omitted —
    #     the case where "skip empty submessages" and "skip default scalars"
    #     look the same and are not. Its own event, because obs_id 0 must
    #     stay unique within a frame.
    minimal = envelope(frame_seq=1237, camera_id=0)
    events.append(
        (
            minimal,
            [
                observation(
                    minimal, obs_id=0, u=0.0, v=0.0, cov_uu=1e-12, cov_uv=0.0,
                    cov_vv=1e-12, bbox_x=0, bbox_y=0, bbox_w=1, bbox_h=1, area_px=1,
                    persistence_count=1, confidence=0.0, local_blob_id=None,
                    evidence_ref=None,
                )
            ],
        )
    )

    # 3. Signs and widths: negative timestamp, negative bbox origin.
    signed = envelope(
        frame_seq=2**63 - 1,
        capture_ts_ns=-(2**62),
        clock_domain=ClockDomain.JETSON_RX,
        camera_id=2**32 - 1,
    )
    events.append(
        (
            signed,
            [
                observation(
                    signed, obs_id=2**32 - 1, u=-0.5, v=-0.5, bbox_x=-(2**31),
                    bbox_y=-(2**31), bbox_w=2**32 - 1, bbox_h=2**32 - 1,
                    area_px=2**32 - 1, persistence_count=2**32 - 1,
                    local_blob_id=2**32 - 1, evidence_ref=None,
                )
            ],
        )
    )

    # 4. Narrowing: five D0-declared `float` fields with unrepresentable
    #    doubles, plus doubles that must cross EXACTLY.
    narrowing = envelope(
        frame_seq=1238,
        time_sync_error_ms=0.1,
        exposure_us=2999.7,
        gain_db=1.1,
        line_readout_us=31.2578125,
    )
    events.append(
        (
            narrowing,
            [
                observation(
                    narrowing,
                    obs_id=7,
                    u=1152.1234567890123,
                    v=647.9876543210987,
                    cov_uu=0.1234567890123456,
                    cov_uv=-0.0123456789012345,
                    cov_vv=0.9876543210987654,
                    confidence=0.3,
                )
            ],
        )
    )

    # 5. A FULL event at the declared bound, every string saturated.
    saturated = envelope(
        frame_seq=1239,
        session_uuid="U" * text(limits.session_uuid_max_size),
        calibration_rev="C" * text(limits.calibration_rev_max_size),
        detector_rev="D" * text(limits.detector_rev_max_size),
    )
    events.append(
        (
            saturated,
            [
                observation(
                    saturated,
                    obs_id=index,
                    evidence_ref="E" * text(limits.evidence_ref_max_size),
                )
                for index in range(limits.observations_max_count)
            ],
        )
    )
    return events


def build_wire_cases() -> FixtureBuild:
    """The adversarial byte fixture. No clip, no detector, no stats."""
    events = wire_case_events()
    observations = [obs for _, event in events for obs in event]
    return FixtureBuild(
        name="wirecases",
        config=DetectorConfig(),
        session_uuid="(various)",
        camera_id=0,
        observations=observations,
        stats=DetectorStats(),
        packets=[
            codec.encode_observation_packet(envelope, event)
            for envelope, event in events
        ],
        injection_sha256="",
    )


# Fixture name -> mover count. The two synthetic fixtures are the SAME scene
# at two crowding levels, so a difference between their results is the
# crowding and nothing else.
SYNTHETIC_FIXTURES = {"sparse": SPARSE_BLOBS, "clutter": CLUTTER_BLOBS}


def scene_clip_dir(root: str | Path, name: str) -> Path:
    """Where a regenerated fixture's pixels live.

    Under a dot-directory beside the fixtures, NOT inside them. The pixels
    are ~18 MB per scene and are fully derivable from a seed; keeping them
    out of the fixture directory means "commit the fixtures" cannot
    accidentally mean "commit 36 MB of regenerable noise". `_regenerate`
    drops a `.gitignore` there as the second belt.
    """
    return Path(root) / ".clips" / name


def ensure_scene_clip(root: str | Path, name: str, seed: int = SCENE_SEED) -> Path:
    """Build a synthetic fixture clip if it is not already on disk."""
    clip_dir = scene_clip_dir(root, name)
    if (clip_dir / "clip.json").exists():
        return clip_dir
    return build_scene_clip(clip_dir, SYNTHETIC_FIXTURES[name], seed=seed)


def _regenerate(root: Path, include_gate: bool, seed: int = SCENE_SEED) -> None:
    root.mkdir(parents=True, exist_ok=True)
    clips_root = root / ".clips"
    clips_root.mkdir(parents=True, exist_ok=True)
    (clips_root / ".gitignore").write_text("*\n", encoding="utf-8")
    for name, blobs in SYNTHETIC_FIXTURES.items():
        clip_dir = build_scene_clip(scene_clip_dir(root, name), blobs, seed=seed)
        build = build_fixture(name, clip_dir, scene_config(name), seed=seed)
        write_fixture(build, root / name)
        print(
            f"{name}: {len(build.observations)} observations, "
            f"{len(build.packets)} packets, "
            f"{build.stats.components_dropped_over_cap} dropped over cap, "
            f"busiest frame offered {build.stats.max_components_offered}"
        )

    if not include_gate:
        return
    if not (GATE_CLIPS / "cam0" / "clip.json").exists():
        raise SystemExit(
            f"gate clip absent under {GATE_CLIPS}; it is a machine-local render "
            "artifact. Re-run with --no-gate to regenerate the synthetic "
            "fixtures alone, or restore the golden clips first."
        )
    # seed=None: the gate clip is a machine-local render artifact, so no seed
    # of ours produced its pixels and the fixture must not claim one.
    gate_build = build_fixture("gate", GATE_CLIPS / "cam0", gate_config(), seed=None)
    write_fixture(gate_build, root / "gate")
    print(
        f"gate: {len(gate_build.observations)} observations, "
        f"{len(gate_build.packets)} packets, "
        f"{gate_build.stats.components_dropped_over_cap} dropped over cap"
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Regenerate the D8 edge fixtures")
    parser.add_argument("--out", default=str(FIXTURE_ROOT))
    parser.add_argument(
        "--no-gate",
        action="store_true",
        help="skip the gate fixture (its clip is a machine-local artifact)",
    )
    # Explicit, per the standing determinism rule: every artifact-producing
    # command takes a seed, and the artifact records the seed it was built
    # with. A default that lives only in the source is a seed nobody can see
    # from the artifact.
    parser.add_argument("--seed", type=int, default=SCENE_SEED)
    args = parser.parse_args(argv)
    _regenerate(Path(args.out), include_gate=not args.no_gate, seed=args.seed)


if __name__ == "__main__":
    main()
