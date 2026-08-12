"""C1 injection: U8 luma frames pushed into the RV1106 daemon.

Stage C1 of ``SYNTHETIC_PIPELINE_DESIGN.md`` section 6: app-layer Y into the
board's IVE GMM2/CCL path. It skips CSI, the ISP and AE, and it has no real
capture clock — so the timestamps it carries are FABRICATED, and this module
exists as much to keep that fabrication honest as to move the pixels.

The stream is one byte format used two ways, "over Ethernet or from local
storage": written to a file the daemon reads with ``--inject-file``, or sent
down a TCP socket the daemon reads with ``--inject-listen``. Same bytes, same
parser, so a file replay and a wire replay cannot diverge.

Format (``SWIJ``, version 1), network byte order throughout — the same rule
as the frozen measurement wire, so the project has ONE byte-order convention
and the daemon needs no second one:

    session header   magic "SWIJ" | version | flags | declared session metadata
    frame record     magic "SWIF" | seq | capture_ts_ns | declared sync error
                     | width | height | payload_len | payload_len bytes of Y
    trailer          magic "SWIE" | frame count

Every record carries its own magic. A stream truncated mid-payload therefore
fails at the next record instead of reading a row of pixels as a header, and
a daemon that mis-tracks its offset by one byte stops rather than detecting
on garbage.

**Injection happens at PROCESSING resolution.** The harness downscales with
the same ``cv2.INTER_AREA`` path the host oracle uses and DECLARES the
full-resolution grid in the session header, so the daemon reports centroids
in full-res coordinates through the D0 scaling law without ever running a
scaler of its own. That is deliberate: the board's ISP self-path and RGA
downscale are not on this phase's scored path (C2/C3 are parked), and
injecting pre-scaled frames removes a divergence D8 is not measuring. The
board benchmark sweeps the three D4 resolutions by injecting each of them.

**The clock is fabricated and says so.** ``capture_ts_ns`` is scene time from
the clip plus a DECLARED offset, drift and jitter (:class:`PtsProfile`), and
``time_sync_error_ms`` carries the magnitude of exactly that perturbation,
frame by frame — the same honesty rule and the same arithmetic as the D6
clock injector (``faults/injectors.inject_clock_error``), so the project has
one definition of an honest declaration. The declared clock domain must not
be a real board clock: see :func:`_check_domain`.
"""

from __future__ import annotations

import argparse
import struct
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from skyweave2.contracts import ClockDomain
from skyweave2.detector.config import DetectorConfig
from skyweave2.eval.clips import iter_frames, read_clip_meta

INJECTION_MAGIC = b"SWIJ"
FRAME_MAGIC = b"SWIF"
END_MAGIC = b"SWIE"
INJECTION_VERSION = 1

# ">" everywhere: network byte order, the same rule as transport/wire.py, and
# no struct padding, so the C reader can hard-code the same offsets.
#   magic, version, flags, reserved,
#   camera_id, full_w, full_h, proc_w, proc_h, frame_count,
#   fps, exposure_us, gain_db, line_readout_us,
#   clock_domain, len(uuid), len(calibration_rev), len(detector_rev)
_SESSION_FIXED = ">4sBBHIIIIIIdfffBBBB"  # 56 B
#   magic, frame_seq, capture_ts_ns, time_sync_error_ms, w, h, payload_len
_FRAME_FIXED = ">4sQqfIII"  # 36 B
_END_FIXED = ">4sI"  # 8 B

_SESSION_FIXED_LEN = struct.calcsize(_SESSION_FIXED)
_FRAME_FIXED_LEN = struct.calcsize(_FRAME_FIXED)
_END_FIXED_LEN = struct.calcsize(_END_FIXED)

# Bit 0 of the session `flags` byte: this stream's `time_sync_error_ms` is NOT
# the magnitude of the perturbation the harness applied. Set only by the
# deliberate known-lie fixtures that prove the honest path is not vacuous.
# It rides in the artifact itself so a stream carrying a lie says so without
# anyone having to remember which file it was.
FLAG_DECLARATION_OVERRIDDEN = 0x01

# Clock domains an injected frame may claim. NODE_MONO and NODE_PTP are the
# board's own clocks (D0 section 3); C1 has neither, and a fabricated PTS
# wearing one of those labels is the exact lie this phase must not tell.
_INJECTABLE_DOMAINS = (ClockDomain.SYNTHETIC,)


class InjectionStreamError(Exception):
    """A malformed, truncated or foreign injection stream. Never recovered."""


class PtsProfile(BaseModel):
    """Declared fabrication of the capture clock.

    ``offset_ms``, ``drift_ppm`` and ``jitter_ms_sigma`` are the same three
    knobs the D6 clock injector uses, with the same meaning, so a fabricated
    board clock and a fault-injected host clock are describable in one
    vocabulary.

    ``declared_error_ms_override`` is the known-lie switch. Left ``None``
    (the default and the only shipping value) the stream declares the true
    per-frame perturbation magnitude. Set to a number, it declares that
    number instead and the stream is flagged as overridden. It exists so
    tests can prove the honest path actually carries something — a test that
    only asserts "the declaration is present" passes just as well when the
    declaration is a constant.
    """

    model_config = ConfigDict(extra="forbid")

    offset_ms: float = 0.0
    drift_ppm: float = 0.0
    jitter_ms_sigma: float = Field(default=0.0, ge=0.0)
    seed: int = 7
    clock_domain: ClockDomain = ClockDomain.SYNTHETIC
    declared_error_ms_override: float | None = None

    @property
    def is_honest(self) -> bool:
        return self.declared_error_ms_override is None

    def describe(self) -> str:
        """One line for a report table. Always names all three knobs."""
        text = (
            f"offset {self.offset_ms:+.3f} ms, drift {self.drift_ppm:+.1f} ppm, "
            f"jitter sigma {self.jitter_ms_sigma:.3f} ms (seed {self.seed}), "
            f"domain {self.clock_domain.value}"
        )
        if not self.is_honest:
            text += (
                f" — DECLARATION OVERRIDDEN to "
                f"{self.declared_error_ms_override} ms (known lie)"
            )
        return text


def _stable_tag(tag: int | str) -> int:
    """Process-stable integer for a seed tag (faults/injectors.py precedent:
    ``hash()`` on str is PYTHONHASHSEED-salted and silently broke a campaign's
    cross-run reproducibility)."""
    if isinstance(tag, int):
        return tag
    return zlib.crc32(tag.encode("utf-8"))


def _jitter_ns(profile: PtsProfile, camera_id: int, frame_seq: int) -> float:
    """Per-frame jitter, keyed by (seed, camera, frame) rather than drawn from
    a running stream: the value for one frame must not depend on how many
    frames were generated before it, or a partial replay would fabricate a
    different clock than the full one."""
    if profile.jitter_ms_sigma <= 0.0:
        return 0.0
    entropy = [profile.seed] + [
        _stable_tag(tag) for tag in (camera_id, frame_seq, "inject-jitter")
    ]
    rng = np.random.default_rng(np.random.SeedSequence(entropy))
    return float(rng.normal(0.0, profile.jitter_ms_sigma * 1e6))


def fabricate_pts(
    scene_ts_ns: int, profile: PtsProfile, camera_id: int, frame_seq: int
) -> tuple[int, float]:
    """(capture_ts_ns, time_sync_error_ms) for one injected frame.

    The shift is ``offset + drift * scene_ts + jitter`` and the honest
    declaration is its magnitude in milliseconds — the arithmetic of
    ``faults.injectors.inject_clock_error``, verbatim, because two different
    definitions of "honest" would be one too many.
    """
    shift = profile.offset_ms * 1e6 + scene_ts_ns * profile.drift_ppm * 1e-6
    shift += _jitter_ns(profile, camera_id, frame_seq)
    declared = (
        abs(shift) / 1e6
        if profile.is_honest
        else float(profile.declared_error_ms_override)
    )
    return int(round(scene_ts_ns + shift)), float(declared)


def _check_domain(domain: ClockDomain) -> None:
    if domain not in _INJECTABLE_DOMAINS:
        raise InjectionStreamError(
            f"injected frames may not claim clock domain {domain.value}: C1 "
            "fabricates the PTS from scene time and has no board clock at all "
            "(SYNTHETIC_PIPELINE_DESIGN.md section 6). Claiming "
            f"{domain.value} would present a fabricated timestamp as a real "
            "capture clock, which is the one thing this stage must not do"
        )


@dataclass(frozen=True)
class InjectionSession:
    """The declared identity of one injection run."""

    camera_id: int
    session_uuid: str
    full_width: int
    full_height: int
    proc_width: int
    proc_height: int
    frame_count: int
    fps: float
    exposure_us: float
    gain_db: float
    line_readout_us: float
    clock_domain: ClockDomain
    calibration_rev: str
    detector_rev: str
    declaration_overridden: bool = False

    @property
    def scale_x(self) -> float:
        return self.full_width / self.proc_width

    @property
    def scale_y(self) -> float:
        return self.full_height / self.proc_height


@dataclass(frozen=True)
class InjectionFrame:
    frame_seq: int
    capture_ts_ns: int
    time_sync_error_ms: float
    luma: np.ndarray  # (H, W) uint8 at PROC resolution


def encode_session_header(session: InjectionSession) -> bytes:
    """The SWIJ session header for one run.

    Public because a stream is not always a file. ``--inject-listen`` takes the
    same bytes down a socket, and a soak generates frames for an hour rather
    than materialising 80 GB of them, so a caller needs to emit the three
    record kinds itself. :func:`build_injection_stream` is that caller too, so
    the file path and the socket path cannot drift apart.
    """
    _check_domain(session.clock_domain)
    uuid_bytes = session.session_uuid.encode("utf-8")
    cal_bytes = session.calibration_rev.encode("utf-8")
    det_bytes = session.detector_rev.encode("utf-8")
    for name, raw in (
        ("session_uuid", uuid_bytes),
        ("calibration_rev", cal_bytes),
        ("detector_rev", det_bytes),
    ):
        if len(raw) > 255:
            raise InjectionStreamError(
                f"{name} is {len(raw)} UTF-8 bytes; the injection header "
                "declares string lengths in one byte"
            )
    flags = FLAG_DECLARATION_OVERRIDDEN if session.declaration_overridden else 0
    header = struct.pack(
        _SESSION_FIXED,
        INJECTION_MAGIC,
        INJECTION_VERSION,
        flags,
        0,  # reserved, must be zero
        session.camera_id,
        session.full_width,
        session.full_height,
        session.proc_width,
        session.proc_height,
        session.frame_count,
        session.fps,
        session.exposure_us,
        session.gain_db,
        session.line_readout_us,
        int(_DOMAIN_TO_WIRE[session.clock_domain]),
        len(uuid_bytes),
        len(cal_bytes),
        len(det_bytes),
    )
    return header + uuid_bytes + cal_bytes + det_bytes


# The injection plane reuses the D7 ClockDomain numbering rather than
# inventing a second one; codec.py maps the same values onto the wire.
_DOMAIN_TO_WIRE = {
    ClockDomain.SYNTHETIC: 1,
    ClockDomain.NODE_MONO: 2,
    ClockDomain.NODE_PTP: 3,
    ClockDomain.JETSON_RX: 4,
}
_WIRE_TO_DOMAIN = {value: key for key, value in _DOMAIN_TO_WIRE.items()}


def encode_frame_record(frame: InjectionFrame) -> bytes:
    """One SWIF frame record, header and payload."""
    if frame.luma.dtype != np.uint8 or frame.luma.ndim != 2:
        raise InjectionStreamError(
            f"injected frame {frame.frame_seq}: expected 2-D uint8 luma, got "
            f"{frame.luma.dtype} {frame.luma.shape}"
        )
    payload = np.ascontiguousarray(frame.luma).tobytes()
    height, width = frame.luma.shape
    return (
        struct.pack(
            _FRAME_FIXED,
            FRAME_MAGIC,
            frame.frame_seq,
            frame.capture_ts_ns,
            frame.time_sync_error_ms,
            width,
            height,
            len(payload),
        )
        + payload
    )


def encode_trailer(frame_count: int) -> bytes:
    """The SWIE trailer. Its count is checked by the daemon against what it
    actually read, so a stream that stops early is an error and not a short
    clip — which is the whole reason the trailer exists."""
    return struct.pack(_END_FIXED, END_MAGIC, frame_count)


def _resize_to_proc(frame: np.ndarray, proc_width: int, proc_height: int) -> np.ndarray:
    """Downscale exactly the way the host oracle does, or not at all.

    ``runner.run_detector`` resizes with ``cv2.INTER_AREA``; using anything
    else here would make the daemon and the oracle disagree about the PIXELS
    before either of them had a chance to disagree about detection, and the
    fixture tolerance table would be measuring the resampler.
    """
    if frame.shape == (proc_height, proc_width):
        return frame
    from skyweave2.detector.components import _require_cv2

    cv2 = _require_cv2()
    return cv2.resize(frame, (proc_width, proc_height), interpolation=cv2.INTER_AREA)


def build_injection_stream(
    clip_dir: str | Path,
    config: DetectorConfig,
    session_uuid: str,
    camera_id: int = 0,
    profile: PtsProfile | None = None,
    frame_limit: int | None = None,
) -> bytes:
    """The whole injection stream for one clip, as bytes.

    Deterministic by construction (test E3): the only inputs are the clip,
    the detector config, the declared session identity and the PTS profile.
    No wall clock, no RNG that is not seeded from ``profile.seed``, no
    dict-ordering dependence.
    """
    profile = profile or PtsProfile()
    _check_domain(profile.clock_domain)
    meta = read_clip_meta(clip_dir)
    count = meta.frame_count if frame_limit is None else min(frame_limit, meta.frame_count)
    fps = meta.fps or config.fps
    session = InjectionSession(
        camera_id=camera_id,
        session_uuid=session_uuid,
        full_width=meta.width,
        full_height=meta.height,
        proc_width=config.proc_width,
        proc_height=config.proc_height,
        frame_count=count,
        fps=fps,
        exposure_us=config.exposure_us,
        gain_db=0.0,
        line_readout_us=0.0,
        clock_domain=profile.clock_domain,
        calibration_rev=config.calibration_rev,
        detector_rev=config.detector_rev,
        declaration_overridden=not profile.is_honest,
    )
    chunks = [encode_session_header(session)]
    written = 0
    for seq, frame in iter_frames(clip_dir):
        if seq >= count:
            break
        # Scene time exactly as `runner.detect_clip` computes it, so an
        # offset of zero reproduces the oracle's timestamps to the nanosecond
        # and any difference is the fabrication, not a rounding disagreement.
        scene_ts_ns = int(round(seq / fps * 1e9))
        capture_ts_ns, declared_ms = fabricate_pts(scene_ts_ns, profile, camera_id, seq)
        chunks.append(
            encode_frame_record(
                InjectionFrame(
                    frame_seq=seq,
                    capture_ts_ns=capture_ts_ns,
                    time_sync_error_ms=declared_ms,
                    luma=_resize_to_proc(frame, config.proc_width, config.proc_height),
                )
            )
        )
        written += 1
    chunks.append(encode_trailer(written))
    return b"".join(chunks)


# ---------------------------------------------------------------------------
# Reading it back (the harness's own parser; the daemon has a C twin)
# ---------------------------------------------------------------------------


def _read_exactly(stream: BinaryIO, count: int, what: str) -> bytes:
    data = stream.read(count)
    if len(data) != count:
        raise InjectionStreamError(
            f"injection stream truncated while reading {what}: wanted {count} B, "
            f"got {len(data)} B"
        )
    return data


def read_injection_session(stream: BinaryIO) -> InjectionSession:
    fixed = _read_exactly(stream, _SESSION_FIXED_LEN, "the session header")
    (
        magic, version, flags, reserved, camera_id, full_width, full_height,
        proc_width, proc_height, frame_count, fps, exposure_us, gain_db,
        line_readout_us, domain, uuid_len, cal_len, det_len,
    ) = struct.unpack(_SESSION_FIXED, fixed)
    if magic != INJECTION_MAGIC:
        raise InjectionStreamError(
            f"bad injection magic {magic!r}, expected {INJECTION_MAGIC!r}"
        )
    if version != INJECTION_VERSION:
        raise InjectionStreamError(
            f"injection stream version {version}, this build speaks "
            f"{INJECTION_VERSION}"
        )
    if reserved != 0:
        raise InjectionStreamError(f"reserved header field is {reserved}, must be 0")
    if flags & ~FLAG_DECLARATION_OVERRIDDEN:
        raise InjectionStreamError(f"unknown injection flags 0x{flags:02x}")
    try:
        clock_domain = _WIRE_TO_DOMAIN[domain]
    except KeyError as exc:
        raise InjectionStreamError(f"unknown clock domain number {domain}") from exc
    _check_domain(clock_domain)
    session_uuid = _read_exactly(stream, uuid_len, "session_uuid").decode("utf-8")
    calibration_rev = _read_exactly(stream, cal_len, "calibration_rev").decode("utf-8")
    detector_rev = _read_exactly(stream, det_len, "detector_rev").decode("utf-8")
    return InjectionSession(
        camera_id=camera_id,
        session_uuid=session_uuid,
        full_width=full_width,
        full_height=full_height,
        proc_width=proc_width,
        proc_height=proc_height,
        frame_count=frame_count,
        fps=fps,
        exposure_us=exposure_us,
        gain_db=gain_db,
        line_readout_us=line_readout_us,
        clock_domain=clock_domain,
        calibration_rev=calibration_rev,
        detector_rev=detector_rev,
        declaration_overridden=bool(flags & FLAG_DECLARATION_OVERRIDDEN),
    )


def iter_injection_frames(stream: BinaryIO) -> Iterator[InjectionFrame]:
    """Yield frames until the trailer. A missing trailer is an ERROR.

    Stopping at EOF instead would make a stream that was cut short
    indistinguishable from a short clip — the daemon would detect on the
    frames it happened to get and report a clean run.
    """
    seen = 0
    while True:
        head = _read_exactly(stream, 4, "a record magic")
        if head == END_MAGIC:
            (declared,) = struct.unpack(">I", _read_exactly(stream, 4, "the trailer"))
            if declared != seen:
                raise InjectionStreamError(
                    f"injection trailer declares {declared} frames, {seen} arrived"
                )
            return
        if head != FRAME_MAGIC:
            raise InjectionStreamError(
                f"expected a frame or trailer magic, got {head!r}; the stream is "
                "out of step and the next bytes would be read as pixels"
            )
        rest = _read_exactly(stream, _FRAME_FIXED_LEN - 4, "a frame header")
        frame_seq, capture_ts_ns, sync_ms, width, height, payload_len = struct.unpack(
            ">QqfIII", rest
        )
        if payload_len != width * height:
            raise InjectionStreamError(
                f"frame {frame_seq}: payload is {payload_len} B for a "
                f"{width}x{height} luma plane ({width * height} B expected)"
            )
        payload = _read_exactly(stream, payload_len, f"frame {frame_seq} luma")
        yield InjectionFrame(
            frame_seq=frame_seq,
            capture_ts_ns=capture_ts_ns,
            time_sync_error_ms=sync_ms,
            luma=np.frombuffer(payload, dtype=np.uint8).reshape(height, width),
        )
        seen += 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Build a C1 injection stream from a clip (file or stdout)"
    )
    parser.add_argument("clip")
    parser.add_argument("--out", required=True, help="output stream path")
    parser.add_argument("--session-uuid", required=True)
    parser.add_argument("--camera-id", type=int, default=0)
    parser.add_argument("--config", help="DetectorConfig JSON (optional)")
    parser.add_argument("--offset-ms", type=float, default=0.0)
    parser.add_argument("--drift-ppm", type=float, default=0.0)
    parser.add_argument("--jitter-ms-sigma", type=float, default=0.0)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--frames", type=int, default=None)
    args = parser.parse_args(argv)

    import json

    config = (
        DetectorConfig.model_validate(json.loads(Path(args.config).read_text()))
        if args.config
        else DetectorConfig()
    )
    profile = PtsProfile(
        offset_ms=args.offset_ms,
        drift_ppm=args.drift_ppm,
        jitter_ms_sigma=args.jitter_ms_sigma,
        seed=args.seed,
    )
    stream = build_injection_stream(
        args.clip,
        config,
        session_uuid=args.session_uuid,
        camera_id=args.camera_id,
        profile=profile,
        frame_limit=args.frames,
    )
    Path(args.out).write_bytes(stream)
    print(f"wrote {args.out}: {len(stream)} B")
    print(f"fabricated PTS: {profile.describe()}")


if __name__ == "__main__":
    main()
