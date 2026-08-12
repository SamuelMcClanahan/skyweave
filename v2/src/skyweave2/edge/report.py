"""D8 edge report generator.

Two subcommands, the same split as the D7 generator:

    uv run python -m skyweave2.edge.report measure --out docs/d8_evidence.json
    uv run python -m skyweave2.edge.report generate \\
        --evidence docs/d8_evidence.json --out docs/D8_EDGE_REPORT.md

``measure`` replays every regenerable fixture through an ALREADY-BUILT
daemon (`--build-dir`, default `firmware/rv1106/build-host`), runs the
suites, and records what came back — including the SHA-256 of the binary it
measured, so the evidence names the thing it is evidence about.

``generate`` writes the document from the committed fixtures, the DECLARED
tolerances and that evidence file, and is byte-identical for identical
inputs — no wall clock, no environment, nothing that changes between two
runs of the same evidence.

The separation is not tidiness. The tolerance declarations must be committed
BEFORE the first board scorecard run (the brief's anti-tuning rule), so the
generator must be able to write the whole declaration half of this document
with no measurements in hand at all. Run it with no evidence file and it
does exactly that, marking every measured row PENDING.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import asdict
from pathlib import Path

from skyweave2.detector import cap
from skyweave2.detector.config import DetectorConfig
from skyweave2.edge import benchmark, daemon, fixtures, image, metrics, tolerance
from skyweave2.edge.injection import PtsProfile, build_injection_stream
from skyweave2.edge.obsfixture import decode_fixture
from skyweave2.edge.tolerance import DetectorTolerance, compare_to_oracle
from skyweave2.transport import codec, sizing
from skyweave2.transport.wire import DATAGRAM_CEILING_BYTES, WIRE_LIMITS, unframe

V2_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_EVIDENCE = V2_ROOT / "docs" / "d8_evidence.json"
DEFAULT_REPORT = V2_ROOT / "docs" / "D8_EDGE_REPORT.md"

# The three D4 sweep resolutions. The board benchmark (D8.1) fills the
# numbers; the rows exist here now so the report shows what is PENDING
# instead of omitting it. Imported from the runner rather than repeated here:
# two lists of the same three resolutions is one list too many.
D4_RESOLUTIONS = benchmark.D4_RESOLUTIONS


# ---------------------------------------------------------------------------
# measure
# ---------------------------------------------------------------------------


def _load_fixture_observations(name: str):
    import io

    raw = (fixtures.FIXTURE_ROOT / name / "observations.swob").read_bytes()
    return [obs for _, event in decode_fixture(io.BytesIO(raw)) for obs in event]


def _load_fixture_packets(name: str) -> list[bytes]:
    text = (fixtures.FIXTURE_ROOT / name / "packets.hex").read_text(encoding="utf-8")
    return [bytes.fromhex(line) for line in text.split() if line]


def _wire_identity(build_dir: Path | None) -> dict:
    """Encode every committed fixture with the C tool; compare to the host's.

    Recorded rather than asserted in prose. The report's central claim is
    that nanopb and the host codec agree byte for byte, and a hardcoded
    "yes" in the generator would say so whether or not they did.
    """
    out: dict = {}
    for name in ("sparse", "clutter", "gate"):
        fixture = fixtures.FIXTURE_ROOT / name
        if not (fixture / "observations.swob").exists():
            continue
        try:
            tool = daemon.fixture_tool_path(build_dir)
        except daemon.DaemonUnavailable:
            return {}
        completed = subprocess.run(
            [str(tool), "encode", str(fixture / "observations.swob")],
            capture_output=True, text=True, check=False,
        )
        if completed.returncode != 0:
            out[name] = {"identical": False, "detail": completed.stderr.strip()[:400]}
            continue
        produced = [bytes.fromhex(line) for line in completed.stdout.split() if line]
        expected = _load_fixture_packets(name)
        out[name] = {
            "identical": produced == expected,
            "datagrams": len(produced),
            "expected_datagrams": len(expected),
        }
    return out


def _run_suite(selector: str) -> dict:
    """Run a pytest selection and record what it actually did."""
    completed = subprocess.run(
        ["uv", "run", "pytest", selector, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=str(V2_ROOT), capture_output=True, text=True, check=False,
    )
    tail = [line for line in completed.stdout.splitlines() if line.strip()]
    summary = tail[-1] if tail else ""
    counts: dict = {"passed": 0, "failed": 0, "skipped": 0, "error": 0}
    import re

    for key in counts:
        match = re.search(rf"(\d+) {key}", summary)
        if match:
            counts[key] = int(match.group(1))
    return {
        "selector": selector,
        "returncode": completed.returncode,
        "summary": re.sub(r"\x1b\[[0-9;]*m", "", summary),
        **counts,
    }


def _sysctl(name: str) -> int | None:
    """One kernel knob, read from procfs. None where there is no procfs."""
    path = Path("/proc/sys") / name.replace(".", "/")
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _gate_platform() -> dict:
    """WHICH machine produced the suite rows, and with which receive buffer.

    The D8.1 opening entry makes a provisioned Linux server the authoritative
    all-green gate and macOS advisory, and runbook A1 says to RECORD the two
    sysctls it requires. Neither claim had anywhere to live: the evidence file
    carried no host field, so the report could only say "the machine that
    generated this report" — true, and useless for deciding whether the gate
    was the gate.

    Recorded by `measure`, quoted by `generate`. `generate` stays a pure
    function of repo + evidence: it reads this block, it never probes a host.
    """
    import platform

    pretty = ""
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                pretty = line.split("=", 1)[1].strip().strip('"')
                break
    except OSError:
        pretty = ""
    out: dict = {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "distribution": pretty,
    }
    for knob in ("net.core.rmem_max", "net.core.rmem_default"):
        value = _sysctl(knob)
        # A knob with no procfs is NOT-MEASURED with its reason, never a zero:
        # macOS has these under a different name and a different meaning, and
        # a 0 here would read as "the buffer is zero".
        out[knob] = value if value is not None else {
            metrics.NOT_MEASURED: "no /proc/sys on this platform"
        }
    return out


#: The receive buffer the D0 "D8.1 opening" entry requires of the gate machine
#: (W3 pushes 720 datagrams before its first drain, and a small buffer loses
#: them silently). A CONSTANT, compared against, rather than a number inside a
#: sentence: the finding "the report asserts the whole-suite row came from the
#: Linux gate platform, unconditionally" was reachable precisely because 4194304
#: existed in this module only inside the prose that failed to check it.
GATE_RMEM_MAX_BYTES = 4_194_304


def _on_gate_platform(platform_block: dict | None) -> bool:
    """Whether the RECORDED platform is the gate A1 describes.

    Two conditions, both from the D0 entry: a Linux kernel, and a receive
    buffer at or above :data:`GATE_RMEM_MAX_BYTES`. A NOT-MEASURED buffer is
    not a pass — the marker means nobody read it, which is exactly the state
    that cannot support the claim.
    """
    if not platform_block:
        return False
    rmem = platform_block.get("net.core.rmem_max")
    return (
        platform_block.get("system") == "Linux"
        and isinstance(rmem, int)
        and not isinstance(rmem, bool)
        and rmem >= GATE_RMEM_MAX_BYTES
    )


def _suite_is_green(suite: dict | None) -> bool:
    """Whether a recorded suite row is a pass, read off the row itself."""
    if not suite:
        return False
    return (
        suite.get("returncode") == 0
        and not suite.get("failed")
        and not suite.get("error")
        and bool(suite.get("passed"))
    )


def _platform_value(platform_block: dict | None, key: str) -> str:
    """One gate-platform cell, rendered once and used by table AND prose.

    The table and the sentence beneath it disagreeing about the same machine is
    the finding this section earned; sharing the renderer makes that
    impossible rather than merely unlikely.
    """
    value = (platform_block or {}).get(key)
    if isinstance(value, dict):
        return f"{metrics.NOT_MEASURED} ({value.get(metrics.NOT_MEASURED, '')})"
    if value in (None, ""):
        return f"{metrics.NOT_MEASURED} (not recorded)"
    return f"`{value}`" if key.startswith("net.core") else str(value)


def measure(work_dir: Path, build_dir: Path | None = None,
            run_suites: bool = True, seed: int = fixtures.SCENE_SEED) -> dict:
    """Replay every regenerable fixture through the daemon; record the result."""
    import hashlib

    work_dir.mkdir(parents=True, exist_ok=True)
    evidence: dict = {
        "schema": "d8-evidence/4",
        "seed": seed,
        "fixtures": {},
        "gate_platform": _gate_platform(),
    }
    try:
        binary = daemon.daemon_path(build_dir)
        evidence["daemon"] = {
            "path": str(binary),
            "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        }
    except daemon.DaemonUnavailable as exc:
        raise SystemExit(
            f"{exc}\n\n`measure` does not build the daemon; build it first so the "
            "evidence can record exactly which binary produced these numbers."
        ) from exc

    provenance = _container_provenance()
    if provenance:
        evidence["container"] = provenance

    identity = _wire_identity(build_dir)
    if identity:
        evidence["wire_identity"] = identity

    for name, blobs in fixtures.SYNTHETIC_FIXTURES.items():
        config = DetectorConfig.model_validate(
            json.loads((fixtures.FIXTURE_ROOT / name / "config.json").read_text())
        )
        session = json.loads(
            (fixtures.FIXTURE_ROOT / name / "stats.json").read_text()
        )["session_uuid"]
        clip = fixtures.build_scene_clip(work_dir / "clips" / name, blobs, seed=seed)
        stream = work_dir / f"{name}.swij"
        stream.write_bytes(
            build_injection_stream(clip, config, session, profile=PtsProfile())
        )
        run = daemon.run_daemon_on_stream(
            stream, config, work_dir / name, build_dir=build_dir
        )
        if run.returncode != 0:
            raise SystemExit(f"{name}: the daemon failed\n{run.stderr}")
        observations = []
        for packet in run.packets:
            observations += codec.decode_observation_packet(unframe(packet)[1])[1]
        divergence = compare_to_oracle(
            _load_fixture_observations(name), observations,
            tolerance.HOST_SOFT_TOLERANCE,
        )
        stats = dict(run.stats)
        # `fps` is a WALL-CLOCK rate over a file replay that is not paced.
        # It says nothing about the node and would put a number that changes
        # every run into a committed artifact, so it is dropped here rather
        # than reported and then explained away.
        stats.pop("fps", None)
        evidence["fixtures"][name] = {
            "detector": "soft",
            "stats": stats,
            "divergence": divergence.as_dict(),
            "breaches": divergence.breaches(tolerance.HOST_SOFT_TOLERANCE),
            "datagrams_byte_identical_to_oracle": run.packets == _load_fixture_packets(name),
        }

    if run_suites:
        evidence["suites"] = {
            "edge": _run_suite("tests/edge"),
            "full": _run_suite("tests"),
        }
    return evidence


def _container_provenance() -> dict | None:
    """Ask the pinned image what it is, if it exists on this machine."""
    completed = subprocess.run(
        ["docker", "run", "--rm", "--platform", "linux/amd64",
         "skyweave-edge-build:d8.0", "cat", "/etc/skyweave-build-provenance"],
        capture_output=True, text=True, check=False,
    )
    if completed.returncode != 0:
        return None
    out = {}
    for line in completed.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip()
    return out or None


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------


#: The E8 axes, label first. The label is what the anti-tuning test looks a
#: bound up by, so it lives beside the renderer rather than being typed twice.
_BENCHMARK_AXES = (
    ("sustained fps (relative)", "fps_relative"),
    ("peak RSS (relative)", "peak_rss_relative"),
    ("A7 utilisation (relative)", "cpu_utilisation_relative"),
)


def _tolerance_rows(name: str, declared: DetectorTolerance, measured: dict | None):
    fields = (
        ("match radius (full-res px)", "match_radius_px", None),
        ("centroid mean (full-res px)", "centroid_mean_px", "centroid_mean_px"),
        ("centroid p95 (full-res px)", "centroid_p95_px", "centroid_p95_px"),
        ("missed fraction", "missed_fraction", "missed_fraction"),
        ("extra per capture event", "extra_per_event", "extra_per_event"),
        ("count-mismatch fraction", "count_mismatch_fraction", "count_mismatch_fraction"),
    )
    rows = []
    for label, declared_field, measured_field in fields:
        bound = getattr(declared, declared_field)
        if measured is None or measured_field is None:
            value = "—" if measured_field is None else "PENDING"
        else:
            value = f"{measured[measured_field]:.4f}"
        rows.append((label, f"{bound:g}", value))
    return rows


def generate(evidence: dict | None) -> str:
    lines: list[str] = []

    def a(text: str = "") -> None:
        lines.append(text)

    fixture_stats = {
        name: json.loads((fixtures.FIXTURE_ROOT / name / "stats.json").read_text())
        for name in ("sparse", "clutter", "gate")
        if (fixtures.FIXTURE_ROOT / name / "stats.json").exists()
    }
    measured = (evidence or {}).get("fixtures", {})
    container = (evidence or {}).get("container")
    # The image manifest is a COMMITTED repo file, like the fixtures, so it is
    # read here rather than carried through the evidence: `generate` stays a
    # pure function of the repository plus the evidence dict, and an image
    # built on the Linux mirror is describable on a machine with no docker.
    manifest = image.load_manifest()

    # Read out of the committed fixtures, not typed in. Section 9's D8-F7
    # makes claims about these artifacts ("all N observations", "none of them
    # saturates"), and review finding 8 is what happens to a claim that is a
    # literal in this generator: it keeps asserting itself after the thing it
    # describes has moved.
    committed = [
        observation
        for name in fixture_stats
        if (fixtures.FIXTURE_ROOT / name / "observations.swob").exists()
        for observation in _load_fixture_observations(name)
    ]
    committed_count = len(committed)
    committed_max_area = max((obs.area_px for obs in committed), default=0)
    committed_saturated = sum(
        1
        for obs in committed
        if obs.area_px >= cap.CONFIDENCE_SATURATION_AREA_PX
    )

    a("# D8 edge report — real RV1106 in the loop")
    a("")
    a("**Labels.** Everything on the host path — the capacity arithmetic, the")
    a("wire byte-identity verdict, the fixture counters and the host-replay")
    a("divergence — is **Measured** on this host and recomputed by this")
    a("generator on every run. Everything about the BOARD is **Pending**:")
    a("no RV1106 has run this daemon yet, so no benchmark, no soak and no")
    a("deployment-resolution choice is claimed. Numbers carried in from")
    a("earlier phases keep the label they had.")
    a("")
    a("**The tolerance declarations in section 6 were committed BEFORE any")
    a("board scorecard run**, which is what the anti-tuning rule requires. The")
    a("host bounds were also committed before the host replay existed, and")
    a("section 9 records what happened when the first measurement broke one.")
    a("")
    a("Generated by `uv run python -m skyweave2.edge.report generate` —")
    a("byte-identical for identical fixtures, declarations and evidence file.")
    a("")

    # ---------------------------------------------------------------- 1
    a("## 1. What this phase delivered")
    a("")
    a("| Sub-phase | Status |")
    a("| --- | --- |")
    a("| D8.0 host-side: capacity, daemon, injection harness, fixtures, E1-E5 | "
      "**complete on this host** |")
    # Derived from the manifest, not asserted. The first draft of this row said
    # "complete on this host" while section 2.1 said PENDING seventy lines
    # later, which is the literal-claim failure mode this generator keeps
    # being caught by (review 10c finding 1, and again here).
    if manifest is None:
        image_state = "image build TOOLING complete, no image built yet (see 2.1)"
    elif manifest.complete:
        image_state = f"image built, {len(manifest.files)} files hashed (see 2.1)"
    else:
        image_state = f"image build FAILED at `{manifest.failed_stage}` (see 2.1)"
    a("| D8.1-prep (board-free): benchmark and provisioning harness, declared "
      "run-to-run bounds | **complete on this host** |")
    a(f"| D8.1-prep: the flashable image set | {image_state} |")
    a("| D8.1 board bring-up: benchmark, soak, deployment resolution | "
      "**not started — needs a flashed node** |")
    a("| D8.2 board validation: fixture replay, toleranced scorecard, health | "
      "**not started — gated on D8.1** |")
    a("")
    a("The brief bars an agent from starting D8.1 until Samuel confirms the")
    a("flashed node, and that gate still holds. Its 2026-08-10 amendment")
    a("sanctions three items that have no board dependency — the image build,")
    a("the harness, and the declared bounds — and those are the two rows in the")
    a("middle. The image build gets its own row because building the TOOLING")
    a("and running it to completion are different claims, and one row would")
    a("have let the stronger one stand in for the weaker. Nothing below claims")
    a("a board number.")
    a("")

    # ---------------------------------------------------------------- 2
    a("## 2. Build provenance")
    a("")
    a("| Item | Value |")
    a("| --- | --- |")
    if container:
        a("| Container image tag | `skyweave-edge-build:d8.0` |")
        for key in ("base", "luckfox_repo", "luckfox_commit", "toolchain", "gcc",
                    "cmake", "git", "debian"):
            if key in container:
                a(f"| {key.replace('_', ' ').capitalize()} | `{container[key]}` |")
    else:
        a("| Container image tag | `skyweave-edge-build:d8.0` (not present on the "
          "machine that generated this report) |")
    a("| nanopb | 0.4.9, vendored under `firmware/rv1106/third_party/nanopb/` |")
    a("| Generated protobuf sources | `firmware/rv1106/proto/skyweave.pb.{c,h}`, "
      "checked in, bounds from `proto/skyweave.options` verbatim |")
    a("| Board image | see 2.1 |")
    a("")
    a("The image is `linux/amd64` because the Luckfox toolchain binaries are")
    a("x86_64 ELF. On Apple Silicon it runs under emulation; the Linux PC runs")
    a("the same image natively as the mirror the brief calls for. The SDK is")
    a("pinned by COMMIT, not by branch — a branch would make \"the reproducible")
    a("build\" a statement about the day it ran.")
    a("")
    a("**Cross-compile verdict: CLEAN.** `./scripts/build-board.sh` inside that")
    a("image produces `skyweave-edge` and `sw-fixture-tool` as ELF 32-bit ARM")
    a("EABI5 binaries against `/lib/ld-uClibc.so.0`, linking `librve`, `libivs`,")
    a("`librga` and `librockit` — the real IVE, RGA and RKMPI paths, compiled,")
    a("not stubbed. The binaries carry no RPATH, so the node's own loader")
    a("resolves those libraries instead of a build-container path that does not")
    a("exist on it.")
    a("")
    a("Two build notes, both recorded because the next person will hit them:")
    a("")
    a("- `librockit.so` itself needs `libdrm` and `librockchip_mpp`, which live")
    a("  on the BOARD's rootfs and are not in the SDK's sparse checkout, so the")
    a("  link passes `-Wl,--allow-shlib-undefined`. That relaxes undefined")
    a("  symbols in a SHARED LIBRARY only; an undefined symbol in this daemon's")
    a("  own objects is still a link error, which is the check that matters.")
    a("- Under linux/amd64 EMULATION on Apple Silicon, `cc1` and `as` segfault")
    a("  intermittently — different files each time, the same file compiling")
    a("  cleanly on the next attempt. `build-board.sh` retries a bounded number")
    a("  of times and announces every retry. On the native Linux mirror the")
    a("  first attempt succeeds. It is an emulation artifact, not a compiler bug")
    a("  and not a property of this source.")
    a("")

    # -------------------------------------------------------------- 2.1
    a("### 2.1 The flashable image set (D8.1-prep item 1)")
    a("")
    a("Built by `scripts/build-image.sh` in a SECOND pinned container,")
    a("`skyweave-image-build:d8.1`, at the SAME SDK commit as the daemon image.")
    a("Two images because they need different things: the daemon build needs a")
    a("toolchain and three media SDKs, the image build needs the whole SDK and")
    a("the Debian packages its README asks for. Splitting them keeps")
    a("`skyweave-edge-build:d8.0` — the tag this report already quotes for the")
    a("binary D8.0 measured — exactly as it was.")
    a("")
    a("The daemon is deliberately NOT baked into the rootfs. The amendment")
    a("allows hand-start until D8.2, and an image that carried the binary would")
    a("have to be rebuilt whenever the binary moved, which is the wrong")
    a("property for a provenance record during bring-up.")
    a("")
    # Past tense, and titled by what it explains rather than by how far it got.
    # The finding "section 2.1 narrates an abandoned emulated Mac build directly
    # above a manifest table from a different, complete build" is what forced
    # the split: everything from here to the branch is a MECHANICAL lesson about
    # the script, true whether or not an image exists, and every sentence that
    # was a status claim about this item moved into the `manifest is None`
    # branch below, where it is true.
    a("**Why the script sweeps object files: an emulated-Mac attempt, and a")
    a("retry loop that was not enough.** The daemon build is twelve files, so")
    a("`build-board.sh` can retry the whole thing when the emulated `cc1` dies.")
    a("An image build is u-boot, a kernel and a Buildroot rootfs — five orders")
    a("of magnitude more compiler invocations — and the crash leaves a build")
    a("output that EXISTS and is newer than its source. Make then treats it as")
    a("up to date and never rebuilds it, so the next attempt walks past the")
    a("damage and dies at the LINK instead: that attempt ended with `undefined")
    a("reference to stdio_devices`, from a `common/stdio.o` whose compile had")
    a("been interrupted several attempts earlier.")
    a("")
    a("So `build-image.sh` checks the magic number of every object, archive and")
    a("module the failed attempt touched and deletes the ones that are not what")
    a("they claim to be, before retrying. On that Mac it removed 23 and then 53")
    a("truncated outputs between successive u-boot attempts — an observation")
    a("about emulation on that host, not a property of the SDK. On a native x86")
    a("Linux host there is nothing for it to clean up.")
    a("")
    if manifest is None:
        # Status claims about THIS item live only here. With a manifest
        # committed, "how far it got" and "the hours belong on the mirror" are
        # PENDING-era sentences describing a different, abandoned run, and the
        # finding they earned was that they read as a caption for the completed
        # build's table twelve lines below.
        a("**How far the emulated-Mac attempt got, recorded because it is the")
        a("useful number:** u-boot built CLEAN on the third attempt (333 s, two")
        a("crashes cleaned up between attempts). The kernel was still going")
        a("after four crashes when that session ended and was stopped")
        a("deliberately — a benchmark and a build competing for four emulated")
        a("cores would have put host load into the suite numbers this report")
        a("publishes, and W6 is already sensitive to exactly that (section 10).")
        a("The tooling is what this item delivers and it demonstrably works;")
        a("the hours belong on the Linux mirror, where the toolchain does not")
        a("need retrying at all.")
        a("")
        a("**PENDING — no image has been built in this repository.** The")
        a("manifest `firmware/rv1106/image/image-manifest.json` does not exist,")
        a("and this section refuses to describe an image set that was not")
        a("produced. To fill it, from `v2/firmware/rv1106`:")
        a("")
        a("```sh")
        a(f"{image.BUILD_COMMAND}")
        a("```")
        a("")
        a("When a build lands, `report generate` fills this section from the")
        a("manifest — the defconfig table, the per-stage status and every")
        a("SHA-256. Nothing here is hand-written, so a build on the Linux mirror")
        a("is published by copying one JSON file into the repo and regenerating.")
        a("")
    else:
        if not manifest.complete:
            a(f"**BUILD FAILED at stage `{manifest.failed_stage}`.** The manifest")
            a("is committed anyway: a failed build and an absent build look the")
            a("same from outside, and they are not the same thing. The stage")
            a("table below is what happened, and the logs are beside the")
            a("manifest.")
            a("")
        # The paragraph the finding asked for: the tables below are NOT the
        # attempt described above, and the artifact cannot say whose they are.
        a("**The build below is not that attempt.** The attempt above was")
        a("stopped; what follows is read out of a manifest committed by a build")
        a(f"that ran to `{manifest.status}` — a different run, on a machine this")
        a("record does not name. The stage counts, the byte counts and the hashes")
        a("are that run's, and nothing above them describes it.")
        a("")
        a("| Item | Value |")
        a("| --- | --- |")
        a(f"| Image build container | `{manifest.container_tag}` |")
        a(f"| Board config | `{manifest.board_config}` |")
        a(f"| SDK commit | `{manifest.sdk_commit}` |")
        for label, value in manifest.defconfig_rows():
            a(f"| {label} | `{value}` |")
        # Derived from the manifest, never asserted: runbook A2 makes "native
        # x86 Linux" this build's precondition, and the finding was that no
        # artifact in the repo evidences it either way. NOT-MEASURED with its
        # reason while the manifest carries no host block; the recorded values
        # the moment one does.
        if manifest.build_host:
            for key in sorted(manifest.build_host):
                a(f"| Build host {key} | `{manifest.build_host[key]}` |")
        else:
            a(f"| Build host | {metrics.NOT_MEASURED} — the manifest carries no "
              "host block; `build-image.sh` has no `uname`, `arch` or binfmt "
              "probe, and `provenance` describes the CONTAINER, which is "
              "identical native or emulated (D8-F14) |")
        a(f"| Daemon baked into the rootfs | {'yes' if manifest.daemon_baked_in else 'no'} |")
        a(f"| Build status | {'complete' if manifest.complete else '**FAILED**'} |")
        a("")
        a("Stages, in the order `build.sh all` runs them (`save` is omitted: it")
        a("stamps a copy with the wall clock, and this project does not put")
        a("wall-clock values in artifacts). More than one attempt means a")
        a("compile or link step died and the retry resumed after the")
        a("magic-number sweep; on the emulated path that was `cc1`, and the")
        a("script cannot tell that case from any other (D8-F13):")
        a("")
        a("| Stage | Status | Attempts |")
        a("| --- | --- | --- |")
        for stage in manifest.stages:
            a(f"| {stage.get('stage', '?')} | {stage.get('status', '?')} | "
              f"{stage.get('attempts', 1)} |")
        a("")
        if manifest.files:
            a("Every file the build produced, with the SHA-256 the brief asks")
            a("for. The images themselves are gitignored — they are derived and")
            a("large; this table is the record:")
            a("")
            a("| Image file | Bytes | SHA-256 |")
            a("| --- | --- | --- |")
            for item in manifest.files:
                a(f"| `{item.name}` | {item.bytes} | `{item.sha256}` |")
            a("")
            a(f"Total: {len(manifest.files)} files, {manifest.total_bytes} B.")
            a("")
        else:
            a("No image files were produced, so there is nothing to hash.")
            a("")

    # ---------------------------------------------------------------- 3
    a("## 3. The recorded decisions, implemented")
    a("")
    a("Two entries in the D0 decisions log are implemented by this phase and")
    a("nothing else is: the \"D8 opening\" capacity decision, and the \"D8.0")
    a("amendment\" that says what the `confidence` field carries. Both are")
    a("sanctioned in writing before the code moved; neither is an edit.")
    a("")
    a("### 3.1 Capacity (the D8 opening entry)")
    a("")
    budget = sizing.worst_case_budget()
    a("| Item | Before (D7) | After (D8) |")
    a("| --- | --- | --- |")
    a(f"| Datagram ceiling | 1200 B (Provisional) | {DATAGRAM_CEILING_BYTES} B "
      "= 1500 MTU - 20 IPv4 - 8 UDP (Chosen) |")
    a(f"| `ObservationPacket.observations` max_count | 5 | "
      f"{WIRE_LIMITS.observations_max_count} |")
    a(f"| Detector per-frame component cap | none | "
      f"{DetectorConfig().max_components_per_frame} |")
    a("")
    a("Worst case the SCHEMA permits, measured by encoding rather than")
    a("estimated (`transport/sizing.py`): every string at its nanopb")
    a("`max_size`, every varint at its widest, a negative `capture_ts_ns`,")
    a("every optional present.")
    a("")
    a("| Component | Bytes |")
    a("| --- | --- |")
    a(f"| Header | {budget.header_bytes} |")
    a(f"| FrameEnvelope | {budget.envelope_bytes} |")
    a(f"| Each Observation2D | {budget.per_observation_bytes} |")
    a(f"| Declared observation bound | {budget.observation_count} |")
    a(f"| Worst-case datagram | {budget.total_bytes} |")
    a(f"| Headroom | {budget.headroom_bytes} |")
    a("")
    a("The D8 opening entry predicted 251 + 7x163 = 1392 B with 80 B of")
    a(f"headroom. The encoder says {budget.total_bytes} B with "
      f"{budget.headroom_bytes} B, because the planning arithmetic omits the")
    a("repeated field's tag and length overhead, which widens as the packet")
    a("grows. The conclusion is unchanged — 7 fits, 8 does not — and the")
    a("difference is recorded rather than smoothed over: `sizing.py` derives")
    a("the bound by encoding for exactly this reason.")
    a("")
    a("**Invariant, tested (E1): wire `max_count` >= detector cap.** The wire")
    a("bound is what the peer's nanopb ALLOCATES from, so a cap above it would")
    a("build capture events this host encodes happily and the board cannot")
    a("decode. The daemon refuses such a configuration at startup rather than")
    a("discovering it on a cluttered frame.")
    a("")
    a("**Golden byte fixtures: unchanged.** `max_count` is an allocation")
    a("bound, not a wire value, so raising it moved no encoded byte. The D7")
    a("`wire_golden/*.hex` files are untouched and still gate W1.")
    a("")
    a("### 3.2 Wire confidence (the D8.0 amendment)")
    a("")
    a("The D8.0 hand-back surfaced three components of the system putting")
    a("different numbers in one wire field (finding D8-F6). The amendment")
    a("settles which number:")
    a("")
    a(f"    confidence = min(1.0, area_px / "
      f"{cap.CONFIDENCE_SATURATION_AREA_PX!r})   # area_px at PROC resolution")
    a("")
    a("| Item | Value |")
    a("| --- | --- |")
    a("| Definition | `detector/cap.py::component_confidence`, once |")
    a("| Reported by | `detector/runner.py`, by CALLING it |")
    a("| Ranked by | `detector/cap.py::rank_key`, level 1 |")
    a("| Mirrored in | `firmware/rv1106/src/sw_pipeline.c`, integer-safe |")
    a(f"| Saturates at | {cap.CONFIDENCE_SATURATION_AREA_PX:g} px, proc "
      "resolution |")
    a("")
    a("It is not a probability and the Jetson must not read it as one. A")
    a("background model plus connected components has no appearance model; area")
    a("is the only evidence it holds, so this is a monotone restatement of how")
    a("much of the frame said something happened. It is also resolution-")
    a("dependent by construction — the same object at 1536x864 and at 1152x648")
    a("does not get the same number — which is honest for a per-node quantity")
    a("and one more reason not to calibrate anything on it. The NPU appearance")
    a("gate (node design section 9, phase 2) is what would make it real.")
    a("")
    a("`persistence_count` is deliberately absent from the formula: it is")
    a("already its own wire field, and folding it in would double-count the")
    a("same evidence inside a value the fusion side may later weight by.")
    a("")
    a("**Integer-safe** in `sw_pipeline.c` means three specific things, because")
    a("this value sits inside an ABSOLUTE byte gate and one ULP is a failure:")
    a("the saturation test is an integer comparison (`area_px >= 50`), which no")
    a("rounding mode or evaluation width can reach; `(double)area_px` is exact")
    a("for every `uint32_t`; and the division happens in double and is narrowed")
    a("to binary32 exactly once, at the wire field — the same two roundings, in")
    a("the same order, as Python's double division followed by protobuf's")
    a("`float` store.")
    a("")
    a("Only the middle one is load-bearing TODAY, and the report says so rather")
    a("than implying three necessities. Checked exhaustively: an all-float form")
    a("agrees bit-for-bit with this one over every area the clamp admits, and")
    a("the two first diverge at 2^24 + 1 — where it is the operand conversion")
    a("that loses, which the clamp puts out of reach. The shape above is")
    a("correctness by construction, not a measured difference, and that is the")
    a("point: the divisor and the saturation area are decisions, and decisions")
    a("move. The adversarial review caught the first draft of this passage")
    a("asserting the stronger claim (10c, finding 4).")
    a("")
    a("**Fixtures regenerated**, with the log entry as the recorded reason: the")
    a(f"field's VALUE changed on {committed_count - committed_saturated} of the "
      f"{committed_count} committed observations —")
    a("everything below the saturation area, which after the amendment is")
    a("where the value stops being 1.0. The `golden/` manifests are not")
    a("affected — this touches D8 fixtures only — and the byte-identity gate")
    a("re-applies AFTER regeneration, unchanged and still absolute (section 4).")
    a("")

    # ---------------------------------------------------------------- 4
    a("## 4. Wire byte-identity (the absolute gate)")
    a("")
    a("For identical observation inputs, the daemon's nanopb encoding must be")
    a("byte-identical to the host codec. Both encoders are handed the same")
    a("`observations.swob` bytes — a JSON fixture would have let a reader's")
    a("rounding masquerade as an encoder's disagreement.")
    a("")
    a("These are the POST-AMENDMENT fixtures (section 3.2). The gate is the")
    a("same gate: regenerating the inputs does not soften it, and the max")
    a("datagram column below is unchanged from before the regeneration because")
    a("`confidence` is a fixed-width `float` — a different value in it costs")
    a("the same five bytes, so the capacity arithmetic in section 3.1 is")
    a("untouched by the amendment.")
    a("")
    a("| Fixture | Capture events | Observations | Max obs/event | Max datagram B | "
      "nanopb == host codec |")
    a("| --- | --- | --- | --- | --- | --- |")
    identity = (evidence or {}).get("wire_identity", {})
    for name in ("sparse", "clutter", "gate"):
        stats = fixture_stats.get(name)
        if stats is None:
            continue
        # MEASURED, never asserted. A hardcoded "yes" here would say the
        # encoders agree whether or not they do, in the one document whose
        # job is to say whether they do.
        row = identity.get(name)
        if row is None:
            verdict = "PENDING (run `report measure`)"
        elif row.get("identical"):
            verdict = f"**yes** ({row.get('datagrams', '?')} datagrams)"
        else:
            verdict = "**NO — investigate**"
        a(f"| {name} | {stats['capture_events']} | {stats['observations']} | "
          f"{stats['max_observations_per_event']} | {stats['packet_bytes_max']} | "
          f"{verdict} |")
    a("")
    a("Plus an adversarial fixture no clip produces: an all-zero bbox origin")
    a("(a PRESENT but empty submessage), `local_blob_id` absent versus 0,")
    a("`evidence_ref` absent versus the empty string, a negative")
    a("`capture_ts_ns` (a full 10-byte varint), negative bbox origins through")
    a("the zigzag path, every string at its declared bound, and a full")
    a(f"{WIRE_LIMITS.observations_max_count}-observation event. Byte-identical "
      "on all of them, in both")
    a("directions, with unknown-field tolerance checked at the C boundary.")
    a("")
    if identity:
        agreed = all(row.get("identical") for row in identity.values())
        if agreed:
            a("**Verdict: EXACT**, measured over "
              f"{sum(row.get('datagrams', 0) for row in identity.values())} datagrams. "
              "No tolerance was")
            a("needed and none is permitted here.")
        else:
            broken = sorted(k for k, v in identity.items() if not v.get("identical"))
            a(f"**Verdict: DIVERGED on {', '.join(broken)}.** The wire gate is "
              "absolute; this is a")
            a("hand-back blocker, not a tolerance question.")
    else:
        a("**Verdict: PENDING** — no C toolchain on the machine that generated")
        a("this report, so nothing here measured the encoders against each other.")
    a("")

    # ---------------------------------------------------------------- 5
    a("## 5. Host fixture replay (E5)")
    a("")
    if measured:
        a("The C daemon replays each injection stream end to end — injection")
        a("reader, GMM2, opening, CCL, persistence, cap, nanopb, framing — and")
        a("its datagrams are compared against the host oracle's.")
        a("")
        a("| Fixture | Frames | Capture events | Observations | Offered | "
          "Dropped by cap | Frames at cap | Datagrams == oracle |")
        a("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for name in sorted(measured):
            stats = measured[name]["stats"]
            identical = "**yes**" if measured[name][
                "datagrams_byte_identical_to_oracle"] else "no"
            a(f"| {name} | {stats['frames_in']} | {stats['capture_events']} | "
              f"{stats['observations_sent']} | {stats['components_offered']} | "
              f"{stats['components_dropped_over_cap']} | {stats['frames_at_cap']} | "
              f"{identical} |")
        a("")
        exact = all(
            row["datagrams_byte_identical_to_oracle"] for row in measured.values()
        )
        if exact:
            a("**The portable detector is byte-exact against the oracle on this")
            a("host.** That is stronger than the declared tolerance in section 6,")
            a("which remains the contract. It holds because `sw_detect_soft.c` is")
            a("a faithful transcription of the `ive_approx` backend — same")
            a("float32 state, same numpy promotion points, same structuring")
            a("element — and because the build pins `-ffp-contract=off` so no")
            a("compiler fuses a multiply-add the oracle rounds twice.")
            a("")
            a("It says nothing about the board. The hardware GMM2 is a different")
            a("detector with four structural divergences named in section 6, and")
            a("D8.2 measures it against the bounds declared there.")
    else:
        a("**PENDING** — run `python -m skyweave2.edge.report measure` on a")
        a("machine with a C toolchain to fill this section.")
    a("")

    # ---------------------------------------------------------------- 6
    a("## 6. Detector tolerances — DECLARED BEFORE THE BOARD RUNS")
    a("")
    a("The D8 fixture policy is split: the wire is exact, the detector is")
    a("toleranced. These bounds live in `skyweave2/edge/tolerance.py`, test E5")
    a("enforces them, and test E5 also checks that this document quotes the")
    a("same numbers — a declaration that only exists in code is not a")
    a("declaration.")
    a("")
    a("### 6.1 Portable C GMM2 vs the `ive_approx` oracle (host)")
    a("")
    a("| Axis | Declared bound | Measured |")
    a("| --- | --- | --- |")
    host_measured = None
    if measured:
        # The sparse fixture is where the DETECTOR is measured: the cap is
        # inert there, so a difference is a detection difference.
        host_measured = measured.get("sparse", {}).get("divergence")
    for label, bound, value in _tolerance_rows(
        "soft", tolerance.HOST_SOFT_TOLERANCE, host_measured
    ):
        a(f"| {label} | {bound} | {value} |")
    a("")
    a("Measured on the **sparse** fixture, where the cap never bites. That")
    a("choice is section 9's finding D8-F3: on the clutter fixture the cap")
    a("turns a sub-pixel detector difference into a different SUBSET of")
    a("measurements, and mixing the two would report a selection effect as a")
    a("detection error.")
    a("")
    a("### 6.2 Hardware RK_MPI_IVE_GMM2 + IVE_CCL vs the same oracle (board)")
    a("")
    a("| Axis | Declared bound | Measured |")
    a("| --- | --- | --- |")
    for label, bound, _pending in _tolerance_rows(
        "ive", tolerance.BOARD_IVE_TOLERANCE, None
    ):
        a(f"| {label} | {bound} | PENDING (D8.2) |")
    a("")
    a("Wider than the host bounds on every axis because four structural")
    a("divergences are known IN ADVANCE, read out of the SDK headers rather")
    a("than guessed. They are properties of the hardware, not defects, and")
    a("each is named in `firmware/rv1106/src/sw_detect_ive.c`:")
    a("")
    a("| # | Divergence | Why it moves a number |")
    a("| --- | --- | --- |")
    a("| 1 | `rk_mpi_ive.h` documents IVE_CCL as \"Only 8-Connected method is "
      "supported\"; the host oracle uses cv2 connectivity 4 | blobs touching "
      "only diagonally are ONE component on the board and TWO on the host |")
    a("| 2 | `IVE_REGION_S` carries area and the four bbox edges — no centroid | "
      "the centroid is a first moment the A7 takes over the label image, so "
      "the area is the hardware's and the centroid is ours, and they can "
      "disagree about a boundary pixel |")
    a("| 3 | `IVE_CCL_CTRL_S` has `u16InitAreaThr`/`u16Step` and the hardware "
      "RAISES its area threshold until the region count fits 254 | the "
      "effective `min_area_px` MOVES on crowded frames; the daemon reads "
      "`u32CurAreaThr` back every frame and counts the frames it rose |")
    a("| 4 | GMM2's controls are u8q2/u10q0 fixed point | the host's float "
      "learn rate, background ratio and weights are QUANTISED on the way in; "
      "the daemon logs what it actually programmed |")
    a("")
    a("D8.2 reports the measured divergence against these bounds. Exceeding")
    a("one is a finding to investigate and record, not automatically a")
    a("failure — but the bound is what decides which conversation happens,")
    a("and it exists before the data.")
    a("")

    # ---------------------------------------------------------------- 7
    a("## 7. Cap and health behaviour")
    a("")
    clutter = fixture_stats.get("clutter", {}).get("detector", {})
    if clutter:
        a("| Quantity | clutter fixture |")
        a("| --- | --- |")
        a(f"| Scored frames | {clutter['frames']} |")
        a(f"| Frames at the cap | {clutter['frames_at_cap']} |")
        a(f"| Components offered | {clutter['components_offered']} |")
        a(f"| Components emitted | {clutter['components_emitted']} |")
        a(f"| Components dropped by the cap | "
          f"{clutter['components_dropped_over_cap']} |")
        a(f"| Busiest frame offered | {clutter['max_components_offered']} |")
        a("")
    a("Every dropped component is counted in three places: the detector's own")
    a("stats, the daemon's `--stats` JSON, and the 1 Hz health packet's")
    a("`drops`. No capture event was unencodable on any fixture — which is the")
    a("entire point of the cap, since before it a cluttered frame was a loud")
    a("failure and a lost measurement (D7-F1).")
    a("")
    a("`HealthPacket.drops` is a TOTAL — frames the detector failed, plus")
    a("components the cap removed, plus events that could not be encoded — and")
    a("finding D8-F2 records why it is not a breakdown.")
    a("")

    # ---------------------------------------------------------------- 8
    a("## 8. Board benchmark and deployment resolution — PENDING (D8.1)")
    a("")
    a("The benchmark sweeps GMM2 at the three D4 resolutions and the surviving")
    a("one gets a one-hour soak. All three pass the D4 HOST gate, so the board")
    a("ceiling is what picks the deployment resolution; the intersection")
    a("argument and the choice become a decisions-log entry (label: Chosen,")
    a("benchmark numbers Measured).")
    a("")
    a("Two different absences appear in the table below and they are not the")
    a("same claim. PENDING means no board has run this yet. NOT-MEASURED means")
    a("the quantity has no reading on the machine that would run it — the")
    a("brief's own word for the stubbed collectors, and the harness emits it as")
    a("a marker rather than a zero.")
    a("")
    a("The source mode is a column rather than a footnote: three of the four")
    a("sources are injection sources, so \"injected\" does not identify the")
    a("run, and the byte rate the DETECTOR was fed is a different number from")
    a("the one the link carried whenever the source loops.")
    a("")
    a("| Resolution | Source mode | Source byte rate | Sustained fps | Peak RSS | "
      "DDR bandwidth | A7 utilisation | Thermals | Verdict |")
    a("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for width, height in D4_RESOLUTIONS:
        a(f"| {width}x{height} | PENDING | PENDING | PENDING | PENDING | PENDING | "
          "PENDING | PENDING | PENDING |")
    a("")
    a("| Soak | Value |")
    a("| --- | --- |")
    a("| Resolution | PENDING |")
    a("| Source mode | PENDING |")
    a("| Duration | 1 h (declared) |")
    a("| Frames | PENDING |")
    a("| Drops | PENDING |")
    a("| Thermal drift | PENDING |")
    a("")
    a("Two DECLARED SYSTEMATICS travel with any row whose source mode is")
    a("`inject-ram`. They are quoted here from the constants the harness")
    a("writes into every run record, not retyped: a declaration that exists")
    a("only in code is not a declaration, and one that exists only in prose is")
    a("not enforced.")
    a("")
    a(f"> {benchmark.DDR_PROFILE_NOTE}")
    a("")
    a(f"> {benchmark.RAM_LOOP_SCENE_NOTE}")
    a("")
    a("Both are recorded beside every result and folded into NO bound. They")
    a("appear in no tolerance table and in no bound row, because a systematic")
    a("absorbed into a tolerance stops being visible and starts being")
    a("permission — the two-error-channel rule, applied to a source.")
    a("")
    a("The intersection argument, stated now so the choice cannot be made by")
    a("whichever number is prettiest later: the deployment resolution is the")
    a("HIGHEST of the three that (a) sustains 30 fps on the board with margin,")
    a("(b) fits the node's memory and DDR budgets, and (c) keeps the D4")
    a("centroid tripwire clear at 0.75 px full-res sigma. D4 measured all")
    a("three clear on the host (worst-axis 0.141 px, Modeled), so only (a) and")
    a("(b) can bind — and if none of the three satisfies (a), that is a")
    a("finding about the node, not a licence to relax the gate.")
    a("")
    a("E8 (benchmark reproducibility: two runs, same config, within declared")
    a("run-to-run bounds) is board-gated and runs with this section. Its bounds")
    a("are declared in 8.1, before it.")
    a("")

    # The RAM-loop budget arithmetic. Deliberately placed HERE — inside section
    # 8 and BEFORE `### 8.1` — because the E8 and E5 table gates scan from a
    # heading to the next line starting with `#` and store rows last-write-
    # wins. Under 8.1 a row whose first cell collided with a bound label would
    # silently replace a declared anti-tuning bound, which is a corruption
    # those helpers have a recorded history of.
    ram_budget_mb = benchmark.RAM_LOOP_BUDGET_BYTES // 1_000_000
    a("**The RAM-loop budget (runbook A4).** A node-local clip preloaded into")
    a("DDR removes the link from the source path, and the check that makes it")
    a("legitimate is whether the clip, the detector's own state AND the daemon's")
    a(f"fixed footprint fit the declared {ram_budget_mb} MB line. Every cell")
    a("below is DERIVED ARITHMETIC")
    a("over the allocator sizes in `sw_detect_ive.c` and the clip geometry — no")
    a("reading of any kind produces them, on any machine, so none of them is")
    a("Measured. The daemon sums the SAME THREE TERMS — clip, detector state,")
    a("fixed — from the allocators it actually calls and REFUSES to start when")
    a("it is over, so this is a check that executes rather than a claim. Its")
    a("detector term is not identical to this table's on the IVE arm, and the")
    a("next paragraph is that difference rather than a footnote to it.")
    a("")
    blob_bound = benchmark.IVE_BLOB_DECLARED_BOUND_BYTES
    a("**The daemon's detector term for the IVE arm is STRICTLY LARGER than the")
    a("column below, by one `IVE_CCBLOB_S`.** `ive_footprint_for` sums four U8")
    a("planes, the model store AND that blob; this table counts the first two.")
    a("The blob is an SDK type that appears nowhere in this checkout and the IVE")
    a("arm does not compile off the node, so the term cannot be written here —")
    a(f"it is DECLARED at an upper bound of {blob_bound:,} B")
    a("(`IVE_BLOB_DECLARED_BOUND_BYTES`, an order of magnitude above the 254")
    a("regions `IVE_MAX_REGION_NUM` caps a blob at) rather than left as a silent")
    a("zero. Every IVE cell below is therefore a LOWER BOUND on the board's own")
    a("sum. At these three grids that changes nothing: each row's slack against")
    a("the budget is wider than the declared bound, so counting the blob would")
    a("neither shorten a clip nor break a fit, which")
    a("`test_the_omitted_ive_blob_term_cannot_move_a_derived_clip_length`")
    a("asserts against this table's own grids. The SOFT arm has no such gap and")
    a("is pinned against the daemon's own printed footprint by")
    a("`test_the_budget_check_refuses_rather_than_fitting_the_number`.")
    a("")
    f_allow = benchmark.DAEMON_STRUCT_ALLOWANCE_BYTES
    a("**The fixed column is a DECLARED UPPER BOUND, not a derivation.** The")
    a("daemon's `fixed` is `inject.luma_capacity` — one luma frame,")
    a("`proc_w * proc_h`, exact and target-independent — plus five `sizeof()`")
    a("terms over its own structs. Struct layout is a property of the target")
    a(f"ABI, so the harness declares {f_allow:,} B for that residue rather than")
    a("transcribing five C structs into Python, which would be a second copy")
    a("of the arithmetic that runs — the failure `sw_detect_ive.c`'s own")
    a("comment names. A bound is enough: as long as it is at or above the")
    a("daemon's residue, a clip this table says fits is a clip the daemon")
    a("accepts. That inequality is READ OFF THE DAEMON'S OWN PRINTED `fixed`")
    a("by `test_the_declared_struct_allowance_bounds_the_daemons_own_fixed_term`")
    a("— no residue figure is typed into this generator, where nothing could")
    a("contradict it. **The board's residue is NOT-MEASURED** until C3 prints")
    a("it: that test runs against the HOST build, so what it proves is a bound")
    a("on the board's residue, not the board's value, and re-running it on the")
    a("board is the C3 step that closes the gap.")
    a("")
    a("Before this, the harness subtracted only the detector: the published")
    a("1152x648 row said 174 frames and `fits`, and the daemon refused it at")
    a("startup by 517,856 B — the luma term alone at that grid is 746,496 B")
    a("against 249,856 B of headroom.")
    a("")
    a(f"The {ram_budget_mb} MB is read as DECIMAL bytes "
      f"({benchmark.RAM_LOOP_BUDGET_BYTES:,} B) and as a")
    a("DAEMON-ONLY ceiling. It is the upper end of the with-NPU subtotal in")
    a("`v1/docs/RV1106_EDGE_NODE.md` section 6, \"~120-160 MB\" — a narrower")
    a("thing than that subtotal, which also counts its own \"~30-50 MB\"")
    a("kernel+rootfs row. That section states NO numeric margin anywhere: its")
    a("Notes cells read \"still fits with margin\" and \"very comfortable\",")
    a("which is prose. Which reading governs is an open decision, and it moves")
    a("the top row.")
    a("")
    a("| Resolution | IVE detector state | Clip frames (derived) | Clip bytes | "
      "Daemon fixed (bound) | Total | Budget |")
    a("| --- | --- | --- | --- | --- | --- | --- |")
    for row in benchmark.ram_budget_table():
        a(f"| {row['resolution']} | {row['detector_state_bytes']:,} B | "
          f"{row['clip_frames']} | {row['clip_bytes']:,} B | "
          f"{row['daemon_fixed_bytes']:,} B | "
          f"{row['total_bytes']:,} B | {row['budget_bytes']:,} B |")
    a("")
    full_w, full_h = D4_RESOLUTIONS[0]
    over = (
        benchmark.detector_state_bytes(full_w, full_h, "ive")
        + benchmark.ram_clip_bytes(full_w, full_h, 24)
        + benchmark.daemon_fixed_bytes(full_w, full_h)
    )
    a(f"The clip lengths are DERIVED, not chosen. At {full_w}x{full_h} a 24-frame")
    a(f"clip would total {over:,} B against the "
      f"{benchmark.RAM_LOOP_BUDGET_BYTES:,} B")
    a("line and does not clear it, which is why the derived length there is")
    a(f"{benchmark.ram_loop_max_frames(full_w, full_h)}. Each length is the "
      "longest that both fits the budget with")
    a("the detector and the daemon's fixed footprint counted AND makes")
    a("`clip_frames * 1e9 / fps` a whole")
    a("number of nanoseconds — the second condition is a correctness")
    a("requirement, not tidiness: the looped capture timestamps equal an")
    a("unrolled feed's only when it holds. The D0 \"D8.1 opening\" entry's")
    a("\"~24 full-res frames\" was reasoned against the 256 MB PHYSICAL total")
    a("and is silent about the detector's own allocation, so it needs")
    a("amending against whichever line governs.")
    a("")

    # -------------------------------------------------------------- 8.1
    a("### 8.1 Declared benchmark run-to-run bounds (E8)")
    a("")
    a("DECLARED 2026-08-10, BEFORE any board has run the sweep — the")
    a("anti-tuning rule that governs section 6's detector bounds, applied to")
    a("E8. They live in `skyweave2/edge/tolerance.py` as")
    a("`BENCHMARK_RUN_TO_RUN`, and the E8 harness test reads BOTH the constant")
    a("and this table: a declaration that only exists in code is not a")
    a("declaration.")
    a("")
    a("Each bound is a RELATIVE difference, |a - b| / mean, between two runs of")
    a("the same configuration — one bound covering all three resolutions rather")
    a("than three absolute numbers each needing their own justification.")
    a("")
    a("| Axis | Declared bound | Measured |")
    a("| --- | --- | --- |")
    for label, field_name in _BENCHMARK_AXES:
        bound = getattr(tolerance.BENCHMARK_RUN_TO_RUN, field_name)
        a(f"| {label} | {bound:g} | PENDING (D8.1) |")
    a("")
    a("What is deliberately NOT bounded here:")
    a("")
    a("- **the deterministic counters.** Frames, capture events, observations,")
    a("  components offered and emitted, drops: the same frames through the")
    a("  same detector produce the same components, so two runs must agree")
    a("  EXACTLY. A tolerance on those would be permission for a defect.")
    a("  `benchmark.EXACT_COUNTERS` is the list and the comparison uses `==`.")
    a("- **DDR bandwidth and thermals.** They have no bound because they have")
    a("  no measurement yet. Setting one when the board produces them is a")
    a("  decision to record then — while the numbers are still unseen, which is")
    a("  the only time a bound can honestly be set.")
    a("")
    a("The VERDICT is three-valued — `pass`, `fail`, `incomplete` — and the")
    a("third state is the one that matters. Two NOT-MEASURED values agree about")
    a("nothing, so a comparison that lost an axis is not a pass; it is")
    a("incomplete, and the artifact says which. The board scenario that")
    a("produces it is ordinary rather than exotic: no health packet arrives")
    a("(finding D8-F9) so the only bounded fps axis is absent, and a daemon")
    a("that died leaves no stats file so every counter is absent too. A")
    a("two-valued verdict would call that pair \"within the declared bounds\".")
    a("`fail` outranks `incomplete`: a run that breached a bound and also lost")
    a("an axis has still breached a bound.")
    a("")

    # -------------------------------------------------------------- 8.2
    a("### 8.2 The benchmark and provisioning harness (D8.1-prep item 2)")
    a("")
    a("Written and exercised against the HOST-built daemon before any board")
    a("exists, which is what the amendment asks for: the first board session")
    a("should be debugging the board, not this tooling.")
    a("")
    a("| Piece | Module | State |")
    a("| --- | --- | --- |")
    a("| Resolution sweep (unpaced; the ceiling) | `edge/benchmark.py` "
      "`run_sweep` | host-exercised |")
    a("| Soak (paced; the operating point) | `edge/benchmark.py` `run_soak` | "
      "host-exercised |")
    a("| RAM-loop source (preloaded clip, declared frame budget, declared "
      "per-pass PTS stride, optional integer pace) | `firmware/rv1106` "
      "`--inject-ram` + `edge/benchmark.py` `ram_loop_declaration` | "
      "host-exercised; the IVE arm of the budget check is board-gated |")
    a("| E8 comparison | `edge/benchmark.py` `compare_runs` | host-exercised |")
    a("| Metric collectors | `edge/metrics.py` | host-exercised |")
    a("| 1 Hz health listener | `edge/health.py` | host-exercised |")
    a("| Node provisioning (push, verify, start, collect) | `edge/provision.py` "
      "| host-exercised over a local transport |")
    a("| The same over ssh to a node | `edge/provision.py` `SshTransport` | "
      "board-gated |")
    a("")
    a("Where each number in the sweep table comes from, and whether a")
    a("development host can answer it at all:")
    a("")
    a("| Axis | Mechanism | Available |")
    a("| --- | --- | --- |")
    for axis, mechanism, availability in metrics.COLLECTOR_REGISTRY:
        a(f"| {axis} | {mechanism} | {availability} |")
    a("")
    a("A sweep run is UNPACED — frames as fast as the source delivers them, so")
    a("the fps it reports is a throughput ceiling. A soak run is PACED at a")
    a("declared rate and its fps is a duty cycle. The artifact records which,")
    a("because they are different quantities that share a unit.")
    a("")
    a("The soak's scene LOOPS. An hour at 30 fps is 108000 frames, which at")
    a("1536x864 would be a 143 GB stream; the harness sends a short scene")
    a("repeatedly and says so in the artifact, because a background model")
    a("meeting the same pixels again is a real difference and not one to hide")
    a("inside an fps number.")
    a("")

    # ---------------------------------------------------------------- 9
    a("## 9. Findings")
    a("")
    a("### D8-F1 — the cap ranks by confidence, and confidence is area")
    a("")
    a("The D8 opening says the cap keeps \"the top components by descending")
    a("confidence\". After the D8.0 amendment (section 3.2) there IS a")
    a("confidence to rank by — but it is `min(1.0, area_px / 50.0)`, a")
    a("monotone non-decreasing function of area, so \"top by confidence\" and")
    a("\"top by area\" are the same selection. The finding survives the")
    a("amendment; only its wording changed.")
    a("")
    a("Two consequences follow from the monotonicity, and both are load-")
    a("bearing. Every component at or above 50 px ties at 1.0, so the")
    a("confidence sort is a PARTIAL order and the key needs its lower levels:")
    a("`-confidence`, then `-area_px`, then raster order (`detector/cap.py`).")
    a("Area is what separates the saturated components and it is the right")
    a("thing to separate them by — a larger foreground region is a better-")
    a("conditioned centroid, and it matches the edge byte governor's intent as")
    a("closely as a model-free detector can. Raster order exists only to make")
    a("the choice TOTAL, so the C daemon does not have to reproduce an accident")
    a("of Python's sort. And because levels 1 and 2 can tie but never")
    a("disagree, the daemon's separate component-shedding order")
    a("(`sw_detect_soft.c::rank_worse_than`, area and raster only, used when a")
    a("frame offers more than 254 regions) is the same ranking; a confidence")
    a("model that stops being a function of area has to revisit that too.")
    a("")
    a("Consequence if shipped: the cap's selection is deterministic and")
    a("area-driven, and it will stay that way until the NPU appearance gate")
    a("(phase 2 of the node design) gives `confidence` an independent meaning.")
    a("Anyone reading \"top by confidence\" in the decisions log should read")
    a("this finding first, and anyone tempted to read the wire field as a")
    a("detection probability should read section 3.2.")
    a("")
    a("### D8-F2 — the health plane has one drop counter and three causes")
    a("")
    a("The brief requires dropped components to be counted \"in detector stats")
    a("and the health path, never silent\". `HealthPacket` has exactly one")
    a("counter field, `drops`, and adding a second would be a wire change this")
    a("phase is not sanctioned to make — the D8 opening sanctions the capacity")
    a("touch and nothing else.")
    a("")
    a("So `drops` is defined as the TOTAL of measurement items the node did")
    a("not send: frames the detector failed, components the cap removed, and")
    a("capture events that could not be encoded. The daemon keeps the three")
    a("separated in `--stats` and in its log; the Jetson sees the sum.")
    a("")
    a("Consequence if shipped: a node that is dropping measurements says so")
    a("every second, and an operator cannot tell WHY from the wire alone —")
    a("they have to ask the node. Splitting the counter is a D9 lever and a")
    a("one-field wire decision; it is recorded here rather than taken.")
    a("")
    a("### D8-F3 — the cap amplifies a sub-pixel divergence into a different")
    a("subset of measurements")
    a("")
    a("Found by running the first host replay. With ten movers of nearly equal")
    a("area against a cap of seven, two detectors that agreed on every")
    a("centroid to a fraction of a pixel still sent 24% DIFFERENT")
    a("observations: a one-pixel area difference flips which components")
    a("survive the ranking, and the survivors are then disjoint.")
    a("")
    a("That is why the detector tolerance is measured on the **sparse**")
    a("fixture, where the cap is inert, and why the clutter fixture is used")
    a("for the cap, the counters and the health path instead. Reporting them")
    a("together would have logged a selection effect as a detection error.")
    a("")
    a("Consequence if shipped: on a crowded frame the host oracle can predict")
    a("that the board will send seven good measurements, and cannot predict")
    a("WHICH seven. Three nodes looking at the same crowded sky may each")
    a("forward a different subset, which is a fusion-association question for")
    a("D9, not a detector question. It is bounded — the cap keeps the largest")
    a("components, so the target is never the one dropped while decoys survive")
    a("unless the target is genuinely smaller than seven decoys — but it is")
    a("real and it is new.")
    a("")
    a("### D8-F4 — OpenCV's 3x3 \"ellipse\" structuring element is a cross")
    a("")
    a("`cv2.getStructuringElement(MORPH_ELLIPSE, (3, 3))` returns")
    a("`[[0,1,0],[1,1,1],[0,1,0]]`, not a filled 3x3 square. The daemon's")
    a("first transcription assumed the square, which eroded one extra ring off")
    a("every blob: a systematic ~1 px area deficit on every component, a")
    a("centroid shift of up to 1.1 full-res px, and — through D8-F3 — a")
    a("24% divergence in which components survived the cap.")
    a("")
    a("The DECLARED tolerance is what caught it. The bound (0.5 px mean) was")
    a("committed before the first replay; the first measurement came in at")
    a("0.5414 px and breached it. After the fix the divergence is exactly")
    a("zero. A bound chosen after seeing the data would have been set at 0.6")
    a("and the bug would have shipped.")
    a("")
    a("Consequence if shipped: none, because it did not ship. The general")
    a("lesson is the one the fixture policy is built on — a structuring")
    a("element is a detector parameter, not an implementation detail, and")
    a("\"transcribe the oracle\" means transcribing the library's choices too.")
    a("")
    a("### D8-F5 — the daemon's socket counter is not its measurement counter")
    a("")
    a("Measurement datagrams and the 1 Hz health packet leave through the same")
    a("socket, so the sender's `datagrams_sent` is a socket total and answered")
    a("the wrong question: a replay reported one more datagram than it had")
    a("capture events. Caught by the E5 cross-check between the packet log and")
    a("the counters, which exists precisely because the packet log is the")
    a("evidence the rest of E5 rests on.")
    a("")
    a("Consequence if shipped: a D9 operator reconciling \"datagrams sent\"")
    a("against \"observations received\" would have found a permanent")
    a("off-by-N-health-packets discrepancy and gone looking for a lost")
    a("measurement. The stats now report measurement and socket totals")
    a("separately.")
    a("")
    a("### D8-F6 — the detector reported one confidence and the cap ranked by")
    a("another, and nothing in the suite could tell")
    a("")
    a("Late in the phase `runner.py` was found emitting")
    a("`confidence=min(1.0, area_px / 50.0)` — an area-derived confidence —")
    a("directly beneath a comment stating that the value is constant, while")
    a("`cap.py::component_confidence`, the C daemon's `sw_pipeline.c` and every")
    a("committed fixture still used 1.0.")
    a("")
    a("The gap that let it hide is the interesting part. E2 and E5 both feed on")
    a("COMMITTED fixture bytes, so both stayed green: the daemon still agreed")
    a("with the fixtures, and the fixtures no longer agreed with the detector.")
    a("A fresh oracle run reproduced none of the committed packets, and the")
    a("host and the edge were putting different numbers in the same wire field")
    a("— the exact host/edge divergence this phase exists to eliminate.")
    a("")
    a("Two tests now close it: one asserting that the confidence the runner")
    a("REPORTS is the value `component_confidence` returns (the cap's ranked")
    a("quantity and the reported quantity are one quantity), and one asserting")
    a("that a fresh oracle run still reproduces the committed packet bytes. The")
    a("runner now calls `component_confidence` rather than restating a literal,")
    a("so there is one place a real confidence model can ever land.")
    a("")
    a("Consequence if shipped: the cap would have selected on a constant while")
    a("the Jetson received a variable, both looking reasonable in isolation,")
    a("and every committed fixture would have been quietly stale. If an")
    a("area-derived confidence IS wanted, it is a four-part change — "
      "`cap.py`,")
    a("the C daemon, a fixture regeneration with a recorded reason, and D8-F1 —")
    a("and a decision, not an edit.")
    a("")
    a("**Closed by the D8.0 amendment.** It was wanted, the decision was taken")
    a("and recorded, and the four-part change is exactly what shipped: section")
    a("3.2 for the formula and the mirror, this section's D8-F1 for the")
    a("ranking, and regenerated fixtures with the log entry as the reason. The")
    a("interim constant is Rejected in the log rather than deleted from it. The")
    a("two tests named above are unchanged and still the gate.")
    a("")
    a("### D8-F7 — the amendment moved a wire value, and added a branch")
    a("")
    a("Three things worth writing down about implementing D8.0a, because two")
    a("of them are the sort of thing a green suite hides.")
    a("")
    a("**The selection did not change.** Confidence is monotone in area")
    a("(D8-F1), so the components the cap keeps are exactly the ones it kept")
    a("before. What the D8.0a regeneration moved was the `confidence` field on")
    a(f"the {committed_count - committed_saturated} committed observations "
      "below the saturation area — above it")
    a("the value was 1.0 before the amendment and is 1.0 after — and nothing")
    a("else: same components, same `obs_id`s, same centroids, same datagram")
    a("sizes, same counters. That is a claim, so it is a test: E1 pins that")
    a("the cap's survivors are the ones an area-only ranking would pick, and")
    a("it fails the day a confidence model stops being a function of area —")
    a("which is the day the cap starts selecting differently and the fusion")
    a("side needs to hear about it.")
    a("")
    if committed_saturated == 0:
        a("**No committed fixture reaches the saturated branch.** The busiest")
        a(f"component in any of the three is {committed_max_area} px against a "
          f"{cap.CONFIDENCE_SATURATION_AREA_PX:g} px")
        a(f"saturation point, so every one of those {committed_count} "
          "observations exercises the")
        a("division and none exercises the clamp. Below saturation the host and")
        a("the daemon run the same double division; AT and above it they run")
        a("different expressions — Python's `min()` against an integer short-")
        a("circuit. An untested branch inside an absolute byte gate is the shape")
        a("of D8-F6 again, so E5 grew two clips of fat movers, and asserts on")
        a("both that the daemon's datagrams are byte-identical to the oracle's:")
        a("")
        a("- one that STRADDLES the saturation point (components from 38 px to")
        a("  91 px, including 49, 50 and 51), so the threshold constant is")
        a("  pinned rather than merely exceeded. The first version of this test")
        a("  produced 70-90 px only, which left the daemon's `50` free to be")
        a("  any value in 41..69 with the whole suite green — the review caught")
        a("  it, and the boundary assertions are now part of the test;")
        a("- one CROWDED with saturated movers, so more components than the cap")
        a("  admits all tie at confidence 1.0 and the ranking's `-area_px` level")
        a("  is what decides. Nothing else in the suite reaches that: the")
        a("  clutter fixture exceeds the cap but never saturates, and a single")
        a("  fat mover saturates but never exceeds the cap, so the C")
        a("  comparator's second level had no test that could fail.")
        a("")
        a("Neither is a committed fixture. They cover branches, and a fourth and")
        a("fifth fixture would add tolerance-table rows nothing declares.")
    else:
        a(f"**{committed_saturated} of {committed_count} committed observations "
          "now reach the saturated**")
        a(f"**branch** (busiest component {committed_max_area} px against a "
          f"{cap.CONFIDENCE_SATURATION_AREA_PX:g} px saturation")
        a("point). When this report was first written none did, and E5's")
        a("dedicated saturation clip was added for exactly that reason; it")
        a("stays, because a fixture that happens to cover a branch today is not")
        a("a test that it is covered.")
    a("")
    a("**The regeneration surfaced a stale honesty wart.** The committed")
    a("fixtures predated the `--seed` work (review finding 13 below), so")
    a("regenerating them added a `seed` field to every `stats.json` — including")
    a("the gate fixture's, whose pixels are a machine-local render artifact that")
    a("no seed of ours produced. `\"seed\": 7` there would have been a")
    a("determinism claim with nothing behind it, so the gate fixture records")
    a("`null` and `FixtureBuild.seed` is now `int | None`. Small, but it is the")
    a("exact failure mode the label discipline exists to prevent, and it was")
    a("introduced BY this regeneration rather than found in it.")
    a("")

    a("### D8-F8 — C1 injection cannot feed the sweep at 30 fps, at any of the")
    a("three resolutions, over any medium the node has")
    a("")
    a("Found while building the D8.1 benchmark runner, before a board exists to")
    a("hit it with. It is arithmetic, not a measurement, and it is the reason")
    a("the runner records a byte rate on every run.")
    a("")
    a("| Resolution | Bytes per Y frame | Needed at 30 fps |")
    a("| --- | --- | --- |")
    for width, height in D4_RESOLUTIONS:
        a(f"| {width}x{height} | {benchmark.frame_bytes(width, height)} | "
          f"{benchmark.required_mb_s(width, height, 30.0):.1f} MB/s |")
    a("")
    a("Against that, what a node can actually deliver. The node design's own")
    a("figure for the uplink is 100M Ethernet at \"~90 Mbit/s usable\" and an SD")
    a("card of the usual class runs around a fifth of what the top resolution")
    a("asks for:")
    a("")
    a("| Medium | Declared throughput |")
    a("| --- | --- |")
    for medium, rate in sorted(benchmark.SOURCE_MEDIA.items()):
        a(f"| {medium} | {rate:.2f} MB/s |")
    a("")
    a("The smallest of the three resolutions needs twice the link and slightly")
    a("more than the card; the largest needs eight times the link.")
    a("")
    a("So a C1-injected sweep on the board measures **min(detector, source)**,")
    a("and without knowing which, an fps number from it is not a GMM2 ceiling.")
    a("Three consequences are implemented here:")
    a("")
    a("- every run records `input_mb_s` and a `source_verdict` against a")
    a("  DECLARED medium — declared by the operator, because the harness cannot")
    a("  tell an SD card from a tmpfs and a verdict with an assumed denominator")
    a("  is worse than none;")
    a("- the soak LOOPS a short scene rather than streaming an hour of unique")
    a("  frames, which at 1536x864 would be a 143 GB file;")
    a("- **the source lever is now taken**, under the D0 \"D8.1 opening\"")
    a("  entry's sanction, in runbook step A4: `--inject-ram` preloads a SWIJ")
    a("  clip into DDR once and serves it in a loop, so the link is out of the")
    a("  path entirely. The run ends on a DECLARED frame budget")
    a("  (`--ram-loop-frames`) rather than a clock, because every counter in")
    a("  `benchmark.EXACT_COUNTERS` is compared with `==` and a wall-bounded")
    a("  run would make a slow box indistinguishable from a defect. Capture")
    a("  time advances by a per-pass stride the HARNESS declares")
    a("  (`--ram-loop-pts-stride-ns`); the daemon reads no clock on that path")
    a("  and copies `time_sync_error_ms` untouched. The clip, the detector's")
    a("  own allocation AND the daemon's fixed footprint — all three terms the")
    a("  daemon sums, per section 8 — are checked against `--ram-budget-mb`")
    a("  before the frame loop starts, and an over-budget run is REFUSED rather")
    a("  than shortened. Two runs record two rates: `input_mb_s` for what the")
    a("  link carried — the clip crossed it exactly once — and `source_mb_s`")
    a("  for what the detector was actually fed.")
    a("")
    a("What that does NOT remove is the DDR traffic profile: a resident clip")
    a("re-read by the detector is not an ISP write, and the wrap means the")
    a("background model meets pixels it has already seen. Both are declared")
    a("systematics, quoted verbatim in section 8 and recorded beside every")
    a("result, and neither is folded into any bound.")
    a("")
    a("The honest statement for the D8.1 session: with C1 injection over a")
    a("link, a *sustained 30 fps* claim at any D4 resolution cannot be made on")
    a("the board. What CAN be made is a detector-bound ceiling from a")
    a("RAM-resident clip, with the frame count stated — and section 8 states")
    a("it. The derived clip lengths are")
    lengths = ", ".join(
        f"{benchmark.ram_loop_max_frames(w, h)} at {w}x{h}"
        for w, h in D4_RESOLUTIONS
    )
    a(f"{lengths},")
    a("derived arithmetic and not Measured.")
    a("")

    a("### D8-F9 — health and measurement share one port, so a monitor must")
    a("demultiplex")
    a("")
    a("The daemon opens ONE UDP socket (`main.c`: `sw_udp_open(&sender,")
    a("jetson_host, measurement_port)`) and sends observation packets and 1 Hz")
    a("health packets down it. There is no health port. A listener bound")
    a("anywhere else hears silence — which is what the D8.1-prep listener was")
    a("about to be, and the framing header's payload type is what saved it.")
    a("")
    a("Recorded because it is a constraint on the Jetson side in D9, not just")
    a("on this harness: whatever watches node health there is the same process")
    a("that receives measurements, or it is a second reader on a shared socket.")
    a("`edge/health.py` demultiplexes on `PayloadType` and counts what it")
    a("discards, which is the shape that side will need.")
    a("")

    a("### D8-F10 — a corrupt health datagram raised out of the D7 ingest")
    a("adapter, and the same bug was written here before a test caught it")
    a("")
    a("**Closed by the D8.1 opening entry's \"F10 fix\" row (Chosen), in runbook")
    a("step A3.** The finding is kept in full rather than deleted: the fix is")
    a("one `except` clause and the reasoning is the whole value.")
    a("")
    a("`decode_health` raises two unrelated exception types. A bad clock")
    a("domain gives `ProtocolViolation`, which is a `WireError`; a corrupt")
    a("protobuf BODY gives protobuf's own `DecodeError`, which is not. Code")
    a("that catches `WireError` around it is therefore protected against one of")
    a("the two and not the other.")
    a("")
    a("`transport/adapter.py`'s `SocketIngestAdapter.poll` caught exactly")
    a("`WireError` on its health branch, while its MEASUREMENT branch caught")
    a("`Exception` with a labelled rejection and a comment saying why. So a")
    a("single malformed health datagram propagated out of `poll` — on the")
    a("fusion host, in the loop that receives from three nodes, on the port")
    a("health SHARES with measurements (D8-F9). The project's own rule is that")
    a("one corrupt packet may not kill ingest and every drop is labelled; that")
    a("was one packet away from breaking it.")
    a("")
    a("The D8.1 opening entry sanctioned the fix and the runbook's A3 scheduled")
    a("it. It is the measurement branch's own shape — a broad catch with a")
    a("labelled reject, routed through `_reject` so the loud")
    a("`raise_on_reject=True` path is unchanged. The reason string gained the")
    a("exception type (`health_decode:DecodeError`,")
    a("`health_decode:ProtocolViolation`) because a corrupt protobuf and a")
    a("contract violation are different bugs on different sides of the wire,")
    a("and the E7 listener already keeps them apart.")
    a("")
    a("Gated by `tests/transport/test_d81_f10_health_reject.py`: the first test")
    a("drives all three datagrams — corrupt body, unmappable clock domain,")
    a("valid — through one adapter and asserts the labels separately, that the")
    a("good packet still lands, and that the measurement counters never moved;")
    a("the second asserts the loud path still raises `DecodeError`. The health")
    a("branch had NO coverage in the W-series before this, which is why the")
    a("asymmetry survived D7.")
    a("")
    a("The same mistake was made in `edge/health.py` while writing the D8.1")
    a("listener, and `test_a_corrupt_datagram_is_counted_and_does_not_kill_the_")
    a("listener` is what found it — which is the argument for writing the")
    a("harness before the bench session rather than during it.")
    a("")

    a("### D8-F11 — the sanctioned clip length does not survive the budget")
    a("check the same runbook step asks for, and the budget it is checked")
    a("against never counted the detector")
    a("")
    a("Runbook A4 asks for two things that do not both hold. It sanctions a")
    a("RAM-loop clip of \"~24 full-res frames = 72 MB\" (the D0 \"D8.1 opening\"")
    a("F8 row) and it asks that \"clip + daemon footprint stays under 160 MB\".")
    a("Worked from the allocators rather than from either document:")
    a("")
    f11_detector = benchmark.detector_state_bytes(2304, 1296, "ive")
    f11_clip = benchmark.ram_clip_bytes(2304, 1296, 24)
    a("| Term | Bytes at 2304x1296 | Where it comes from |")
    a("| --- | --- | --- |")
    a(f"| IVE detector state | {f11_detector:,} | "
      "`ive_alloc`: four U8 planes at `stride*height`, plus "
      "`plane * model_num * 12` at the compiled-in `model_num = 3`. EXCLUDES "
      "`ive_footprint_for`'s third term, `sizeof(IVE_CCBLOB_S)` — an SDK type "
      "absent from this checkout, declared at "
      f"{benchmark.IVE_BLOB_DECLARED_BOUND_BYTES:,} B in section 8 |")
    f11_fixed = benchmark.daemon_fixed_bytes(2304, 1296)
    a(f"| A 24-frame clip | {f11_clip:,} | "
      "`frames * proc_w * proc_h`, the arena the preload malloc's |")
    a(f"| Daemon fixed | {f11_fixed:,} | "
      "`inject.luma_capacity` (one luma frame) plus a DECLARED "
      f"{benchmark.DAEMON_STRUCT_ALLOWANCE_BYTES:,} B bound on the five "
      "`sizeof()` terms `main.c` adds |")
    a(f"| Total | {f11_detector + f11_clip + f11_fixed:,} | "
      "against a 160,000,000 B line |")
    a("")
    a("The D0 entry is not wrong about what it says: 72 MB IS within 256 MB,")
    a("the node's PHYSICAL total, which is the budget that entry names. It is")
    a("silent about the detector's own allocation, and 160 MB is a different")
    a("budget. Both readings cannot govern the same clip.")
    a("")
    a("**The 160 MB line was never computed against this detector.**")
    a("`RV1106_EDGE_NODE.md` section 6's memory table has no row for a GMM2")
    a("model bank. Its rows are the v1 ISP node's — a full-res NV12 ring, a")
    a("quarter-res stream, RGA scratch, the RKNN runtime — and under RAM-loop")
    a("injection the D8 daemon allocates none of them. What it does allocate —")
    a(f"{f11_detector:,} B at the top resolution — is larger than every row in")
    a("that table put together. The comparison A4 asks for is not like for")
    a("like, in either direction.")
    a("")
    a("A4 also asks that the budget clear 160 MB \"with the margin stated in")
    a("RV1106_EDGE_NODE.md section 6\". No numeric margin is stated there. The")
    a("Notes cells read \"still fits with margin\" and \"very comfortable\" — that")
    a("is prose, and no figure was invented here to satisfy the clause.")
    a("")
    a("What this phase DID, rather than picking whichever number fits: the")
    a("budget is a declared knob (`--ram-budget-mb`, default 160, decimal MB,")
    a("daemon-only, stated in the header comment), the daemon computes")
    a("clip + detector + fixed at startup, logs every term, and REFUSES rather")
    a("than shortening the clip. The harness derives the longest clip that")
    a("clears THAT SAME THREE-TERM SUM and keeps the looped PTS exact, and the")
    a("derivation is in section 8 with every term. The answer at the top")
    f11_top = benchmark.ram_loop_max_frames(2304, 1296, "ive")
    a(f"resolution is {f11_top} frames, not 24.")
    a("")
    a("**The two sums were not the same sum when this finding was first")
    a("written.** Its own text said \"the daemon computes clip + detector +")
    a("fixed\" two paragraphs above \"the harness derives the longest clip that")
    a("clears the budget\", and the harness's budget had no `fixed` term at")
    a("all — it subtracted the detector and nothing else. At 1152x648 that")
    a("published a 174-frame row as fitting which the daemon refused at")
    a("startup, and the pinning test compared the harness's arithmetic against")
    a("itself, so it could not notice. The harness now reserves the daemon's")
    a("`fixed` (section 8 states which half is exact and which half is a")
    a("declared bound), and the gate that keeps them together reads the")
    a("daemon's own printed `fixed` off a real run rather than recomputing it.")
    a("")
    a("Consequence if shipped: an unamended D0 entry and a daemon that refuses")
    a("it. The first board session would meet the refusal, not the entry, and")
    a("the clip length is a resolution decision made before C3 runs — so it")
    a("belongs to the planning session, and it belongs there before Phase B.")
    a("")

    a("### D8-F12 — the image build exits 0 on a build its own manifest")
    a("calls FAILED")
    a("")
    a("`scripts/build-image.sh` writes its manifest from an inline python")
    a("block, and that block re-derives the status: every stage passed but no")
    a("image files were collected, so it sets `status = \"FAILED\"` with")
    a("`failed = \"collect (no image files were produced)\"` — which is exactly")
    a("the right judgement. But that `status` is a PYTHON-local name. The")
    a("shell's `${status}` is still `complete`, so the guard on the last lines")
    a("(`if [ \"${status}\" != \"complete\" ]`) does not fire and the script exits")
    a("0.")
    a("")
    a("It is reachable rather than theoretical: the collect step is")
    a("`cp -f \"${SDK}/output/image/\"*` with its errors swallowed by")
    a("`2>/dev/null || true`, so a build whose stages all \"succeeded\" and whose")
    a("output directory is empty exits successfully, prints a manifest saying")
    a("FAILED, and the operator moves on. The only way to notice is to read the")
    a("manifest — which is why the A2 procedure in this phase did, and why the")
    a("stage table above is derived from the manifest rather than from an exit")
    a("code.")
    a("")
    a("NOT FIXED HERE, deliberately. This phase is sanctioned to make exactly")
    a("one fix (A3's) and this is not it. The fix is one line — export the")
    a("python block's verdict and test THAT — and it is recorded with the lines")
    a("so it is a decision, not an oversight.")
    a("")

    a("### D8-F13 — the image container advertises an emulation check that")
    a("does not exist, and the check it does have misreports after one crash")
    a("")
    a("`docker/Dockerfile.image` says of the build script: \"The script says so")
    a("out loud when it detects emulation.\" It does not. There is no `uname`,")
    a("no `arch`, no qemu or binfmt probe anywhere in `build-image.sh`.")
    a("")
    a("What exists is `grep -q \"internal compiler error\"` over the stage log,")
    a("used only to choose between two console messages after a failure. And")
    a("the stage log is APPENDED across attempts, so once any attempt within a")
    a("stage has produced an ICE, every later failure in that stage is reported")
    a("as \"emulated cc1 died\" whatever actually killed it — including, on a")
    a("native host, a failure that has nothing to do with a compiler.")
    a("")
    a("This is the D8-F6 class: a document and its code disagreeing, found by")
    a("running the thing rather than reading it. Consequence if shipped: an")
    a("operator debugging a native build failure is handed a diagnosis about")
    a("emulation. NOT FIXED HERE for the same reason as D8-F12; the fix to the")
    a("comment is a comment, and the fix to the grep is to scope it to the")
    a("current attempt.")
    a("")

    # The heading and the closing status are DERIVED from the manifest for the
    # same reason the finding exists: a finding about a missing field that goes
    # on asserting the field is missing after a build records it is the
    # literal-claim failure one level up.
    host_recorded = bool(manifest and manifest.build_host)
    if host_recorded:
        a("### D8-F14 — CLOSED: the image manifest now records which machine")
        a("built the image")
    else:
        a("### D8-F14 — the image manifest does not record which machine built")
        a("the image, and A2 makes that machine load-bearing")
    a("")
    a("Runbook A2 says to build \"on the Linux mirror (native x86 Linux; the")
    a("emulated-Mac path is what crashed)\". A manifest records the container")
    a("tag, the SDK commit, the defconfig and a SHA-256 per file. Its")
    a("`provenance` block is the CONTAINER's: a file baked in at `docker build`")
    a("time plus queries of the container's own contents, byte-identical")
    a("whether `docker run --platform linux/amd64` executed natively on x86")
    a("Linux or under emulation on Apple Silicon. D8-F13 names the same missing")
    a("probe from the diagnostics side; one probe closes both.")
    a("")
    if host_recorded:
        a("The manifest committed here carries a `build_host` block, and section")
        a("2.1 prints it. A2's precondition is now evidenced by an artifact")
        a("rather than by the procedure log, which is what this finding asked")
        a("for.")
        a("")
    else:
        if manifest is None:
            a("No manifest is committed here yet, and the script that writes one")
            a("has no host probe to put in it, so A2's stated precondition is")
            a("evidenced by the procedure log and by no artifact in this")
            a("repository.")
        else:
            a("The manifest committed here carries no host block, so A2's stated")
            a("precondition is evidenced by the procedure log and by no artifact")
            a("in this repository. Section 2.1's build-host row says")
        a(f"{metrics.NOT_MEASURED} with that reason rather than leaving the")
        a("question un-asked, and the reader is not invited to infer the answer")
        a("from the emulated-Mac paragraph earlier in that section, which")
        a("describes an attempt that was abandoned.")
        a("")
        a("NOT FIXED HERE, deliberately, for the same reason as D8-F12 and")
        a("D8-F13, plus one of its own: the fix writes a new field into a")
        a("manifest, and the only way to produce a manifest carrying it is")
        a("another image build. The hashes ARE the identity of the bytes and B2")
        a("re-checks them against the flashed node, so a rebuild buys provenance")
        a("for the build host and nothing else. The fix, for whoever runs the")
        a("next build: add `\"build_host\": {\"uname\": ..., \"emulated\":")
        a("<binfmt/qemu probe>}` beside `provenance` in the manifest python")
        a("block. `edge/image.py` already reads `build_host` and section 2.1")
        a("already prints whatever it finds there, so the report side needs no")
        a("further change.")
        a("")

    # ---------------------------------------------------------------- 10
    a("## 10. E-series status")
    a("")
    suites = (evidence or {}).get("suites", {})
    edge_suite = suites.get("edge")
    # ONE status for E1-E5, taken from an actual run of the E-series rather
    # than five hardcoded PASS cells. Per-test verdicts would be five more
    # literals nothing could contradict; the suite either went green or it
    # did not, and that is the claim worth making.
    if edge_suite is None:
        e_status = "PENDING (run `report measure`)"
    elif edge_suite["returncode"] == 0 and edge_suite["failed"] == 0:
        e_status = f"PASS ({edge_suite['passed']} tests)"
    else:
        e_status = f"**FAIL** — {edge_suite['summary']}"
    a(f"E1-E5 run as one suite (`tests/edge`): **{e_status}**")
    a("")
    a("| Test | Content | Status |")
    a("| --- | --- | --- |")
    a("| E1 | cap + max_count + ceiling agree; invariant holds in Python AND at "
      "daemon startup; cluttered frame emits the top 7 with drops counted; a "
      "full event is encodable at the schema worst case; goldens unmoved; the "
      "wire confidence is the declared area formula, is the one the cap ranks "
      "by, and selects what area alone would select | "
      f"{e_status} |")
    a("| E2 | nanopb byte-identity against the host codec on every fixture and "
      "on the adversarial cases, both directions, unknown-field tolerance; "
      "the committed stats files match the schema the writer writes and the "
      f"counters this report publishes | {e_status} |")
    a("| E3 | injection determinism within a process, across processes, and "
      "against the committed digest; Ethernet and storage replay produce the "
      f"same datagrams | {e_status} |")
    a("| E4 | fabricated-PTS honesty end to end; a board clock domain refused "
      f"at both ends; the known-lie path flagged in the artifact | {e_status} |")
    a("| E5 | host fixture replay under the DECLARED tolerance, plus the "
      "byte-exactness result, cap/health behaviour, health decode on the "
      "unchanged host stack, the packet-log cross-check, host/edge identity "
      "across the saturation boundary and with saturated components "
      f"competing for the cap, and report freshness | {e_status} |")
    a("| E6 | board fixture replay through the unchanged v2 stack, toleranced "
      "scorecard | board-gated (D8.2) |")
    a("| E7 | health packets at 1 Hz with real drop counters, on the node | "
      "board-gated (D8.2) |")
    a("| E8 | benchmark reproducibility: two runs, same config, within declared "
      "run-to-run bounds | board-gated (D8.1) |")
    a("")
    a("E1-E5 are the D8.0 hand-back. E6-E8 need hardware and are not claimed.")
    a("")
    a("What the D8.1-prep work adds to that table is HARNESS coverage, not a")
    a("board result, and the distinction is the point:")
    a("")
    a("| Harness test | What it exercises on this host | What it does NOT claim |")
    a("| --- | --- | --- |")
    a("| `test_e7_health_listener.py` | the listener decodes real 1 Hz health "
      "packets off the measurement port, counts labelled rejections, and "
      "reports cadence and drop-counter monotonicity | E7 itself: 1 Hz on a "
      "NODE with real drop counters |")
    a("| `test_e8_benchmark_harness.py` | the sweep runner, the collectors' "
      "NOT-MEASURED discipline, the soak's paced looped feed, and the E8 "
      "comparison including its ability to fail | E8 itself: two runs on the "
      "BOARD within the declared bounds |")
    a("| `test_d81_image_build.py` | the image manifest reader, its refusals, "
      "and that this document quotes the manifest it was given | that an image "
      "has been flashed to anything |")
    a("| `test_d81_provisioning.py` | push, SHA-256 verification on the far "
      "side, detached start, SIGTERM-preserves-counters and collect, over a "
      "local transport | ssh, scp, and that a Buildroot rootfs has "
      "`sha256sum` |")
    a("| `test_d81_ram_loop_source.py` | the RAM-loop source on the HOST "
      "build: the frame budget that ends a run, continuous frame_seq across a "
      "wrap, the per-pass PTS advance, every refusal, the budget check and "
      "the pacing | that any of it has run on a board, and the IVE arm of the "
      "budget arithmetic, which does not compile off the node |")
    a("")
    a("### Full-suite status")
    a("")
    a("Every prior suite plus the E-series, and `ruff check src tests`, on the")
    a("machine that generated this report:")
    a("")
    full_suite = suites.get("full")
    a("| Suite | Result |")
    a("| --- | --- |")
    a(f"| E-series (`tests/edge`) alone | "
      f"{edge_suite['summary'] if edge_suite else 'PENDING'} |")
    a(f"| Whole suite (`tests`) | "
      f"{full_suite['summary'] if full_suite else 'PENDING'} |")
    a("| Whole suite before D8 (recorded at the start of this phase) | "
      "325 passed, 1 skipped |")
    a("")
    # "Both rows" against a three-row table: the third is a figure recorded at
    # the start of the phase, not a run this generator saw. Named precisely so
    # the caveat below can name the same two.
    a("The first two rows are the tail of an actual `uv run pytest -q`, recorded")
    a("by `report measure` into the evidence file — not a number typed into this")
    a("generator, which nothing could contradict. The third is what the suite")
    a("read at the start of this phase.")
    a("")
    a("#### Which machine, and with which receive buffer")
    a("")
    a("The D0 \"D8.1 opening\" entry moved the authoritative gate: the all-green")
    a("suite for a hand-back runs on a provisioned Linux server, macOS is")
    a("advisory, and the VM prerequisite is a kernel receive buffer of at least")
    a("4 MB because that is the environment D7 measured in. Until D8.1 the")
    a("evidence file had no field for any of it, so this document could only")
    a("say \"the machine that generated this report\" — true, and no use at all")
    a("for deciding whether the gate was the gate. `report measure` now records")
    a("it and this table quotes it.")
    a("")
    platform_block = (evidence or {}).get("gate_platform")
    a("| Gate platform | Value |")
    a("| --- | --- |")
    if platform_block:
        for key, label in (
            ("system", "Kernel"),
            ("release", "Kernel release"),
            ("machine", "Architecture"),
            ("distribution", "Distribution"),
            ("net.core.rmem_max", "`net.core.rmem_max`"),
            ("net.core.rmem_default", "`net.core.rmem_default`"),
        ):
            a(f"| {label} | {_platform_value(platform_block, key)} |")
    else:
        a("| every row | PENDING (run `report measure`) |")
    a("")
    # Names its subject rather than relying on position: "the two rows above"
    # pointed at this table, which renders one row when the block is absent and
    # six when it is present, and never two.
    a(f"A `net.core.rmem_max` under {GATE_RMEM_MAX_BYTES} makes the two measured")
    a("suite rows in **Full-suite status** a weaker claim than they look: W3")
    a("pushes 720 datagrams before its first drain and fails on a COUNT")
    a("mismatch rather than an error, so a small buffer is a silent loss that")
    a("reads as a test failure about evidence handling. The value is published")
    a("here so a reader can check it instead of trusting it.")
    a("")
    skipped = (full_suite or {}).get("skipped") or 0
    if skipped:
        a(f"The {'skip' if skipped == 1 else 'skips'} in that row "
          f"{'is' if skipped == 1 else 'include'} the v1 cross-check, and it is")
        a("an artefact of HOW this generator runs the suite rather than of the")
        a("machine it ran on: `_run_suite` invokes `uv run pytest <selector>`")
        a("with no `PYTHONPATH`, so `test_v1_projection_agrees_with_frozen_"
          "convention`'s")
        a("`importorskip` on `skyweave.fusion.geom` fires. The project's second")
        a("documented invocation, `PYTHONPATH=../v1/src uv run pytest`, runs it")
        a("— and on the gate platform that invocation is where the whole suite")
        a("has NO skips at all. Do not read this row's skip as a missing")
        a("machine-local artifact; that is a different skip, and on a machine")
        a("carrying `output/` it does not fire.")
        a("")
    a("### A host-load observation on W6, recorded because it cost time")
    a("")
    a("Earlier in this phase, with the machine carrying a sustained load")
    a("average near 4 from macOS background indexing (`photoanalysisd`,")
    a("`mediaanalysisd`, `FPCKService`),")
    a("`test_w6_wallclock.py::test_loopback_packet_age_is_measured_and_within"
      "_the_declared_budget` failed repeatedly and reproducibly at a loopback")
    a("scheduling-jitter p95 of ~10.03 ms against its 10.0 ms budget. It")
    a("passed on the same machine once quiet.")
    a("")
    # "and the run recorded above is a clean one" was a claim about the suite
    # row this generator itself prints, asserted whatever that row said — the
    # same shape as the gate claim below, five paragraphs apart. Read off the
    # row instead.
    if _suite_is_green(full_suite):
        a(f"The run recorded above is a clean one: `{full_suite['summary']}`.")
    elif full_suite:
        a("The run recorded above is NOT a clean one —")
        a(f"`{full_suite['summary']}` — so whatever this paragraph says about")
        a("quiet machines, the row a reader can see is the row that governs.")
    else:
        a("Whether the run recorded above is a clean one is PENDING: no suite")
        a("has been recorded into the evidence file.")
    a("")
    a("It was checked against the PRE-D8 tree — same failure, same number — so")
    a("nothing in this phase moved it, and the budget was NOT widened to make")
    a("it green. Widening it would be the gate-weakening this project forbids,")
    a("and `rig_replay_speed` is DERIVED from that budget.")
    a("")
    a("What the episode says is that D7's declared headroom (\"roughly 2x the")
    a("LOADED figure\") no longer holds on this host under ordinary background")
    a("indexing. That is a planning-session decision, not an edit: either")
    a("re-measure and confirm the budget, or record a new loaded-host figure")
    a("and re-derive the rig speed from it.")
    a("")
    a("**D8.1-prep adds data points, and they are worse than the D8.0")
    a("write-up.** It failed reproducibly while the emulated image build held")
    a("four cores, which is the expected direction. But on an OTHERWISE QUIET")
    a("machine it went one clean run, then 10.01 ms, then 10.04 ms, and then")
    a("10.04 ms again running ALONE — against a 10.0 ms budget. So \"it passes")
    a("on the same machine once quiet\" is no longer true: the margin on that")
    a("host is under half a percent and mostly on the wrong side.")
    a("")
    a("**That is the observation D8.1 answered by moving the gate, not the")
    a("budget.** The D0 \"D8.1 opening\" entry makes a provisioned Linux server")
    a("the authoritative all-green suite gate — \"where W6 passes on merit;")
    a("macOS results remain advisory\" — and runbook A1 executes it.")
    a("")
    # DERIVED from the gate-platform block and the suite row, not asserted. The
    # first draft of this paragraph said "the whole-suite row above is that
    # machine's" unconditionally, four paragraphs under a table that could read
    # PENDING or Darwin and a row that could read one failure — the
    # literal-claim failure mode this generator keeps being caught by (review
    # 10c finding 1, 10d finding 1, and again here, in the one section whose
    # whole subject is provenance). `generate` still probes nothing: both
    # predicates read the evidence dict.
    on_gate = _on_gate_platform(platform_block)
    row_green = _suite_is_green(full_suite)
    suite_summary = (full_suite or {}).get("summary") or "PENDING"
    system = _platform_value(platform_block, "system")
    rmem = _platform_value(platform_block, "net.core.rmem_max")
    if on_gate and row_green:
        a("**The whole-suite row above is that machine's.** The gate-platform")
        a(f"table records {system} with `net.core.rmem_max` {rmem} — at or above")
        a(f"the {GATE_RMEM_MAX_BYTES} A1 requires — and the row reads")
        a(f"`{suite_summary}`. The paragraphs above are now an advisory-host")
        a("record rather than an explanation of a red row.")
    elif on_gate:
        a("**The whole-suite row above is that machine's, and it is not green:**")
        a(f"`{suite_summary}`. The gate ran where A1 says it should and still")
        a("reads a failure, so W6 is NOT retired by it and the paragraphs above")
        a("stand until that row is a pass on this platform.")
    elif platform_block:
        a("**The whole-suite row above is NOT that machine's.** The gate-platform")
        a(f"table records {system} with `net.core.rmem_max` {rmem}, which is not")
        a(f"the Linux gate at or above {GATE_RMEM_MAX_BYTES} that A1 requires;")
        a(f"the row reads `{suite_summary}`. These rows are an ADVISORY host's,")
        a("W6 is not retired, and the paragraphs above are still the explanation")
        a("of whatever that row reads. Re-run `report measure` on the gate")
        a("platform before this document is a hand-back artifact.")
    else:
        a("**Whether the whole-suite row above is that machine's is NOT")
        a("RECORDED.** The evidence file carries no gate-platform block, so this")
        a("document cannot say which host produced the row — the state the")
        a("section above exists to remove. W6 is not retired by an unrecorded")
        a("platform; run `report measure` on the gate platform.")
    a("")
    a("The honest scope statement has also changed, and pretending otherwise")
    a("would be the D8-F6 mistake one level up. The D8.1-prep write-up argued")
    a("that nothing under `transport/` differed by a byte, so W6's inputs were")
    a("identical to the tree where it passed. **Phase A breaks that argument:**")
    a("A3 changes `transport/adapter.py`. What it changes is one `except`")
    a("clause on the HEALTH branch of `SocketIngestAdapter.poll`; the W6")
    a("loopback rig sends observation datagrams and no health datagrams, so it")
    a("never reaches that branch. That is a weaker argument than byte-identity")
    a("and it is stated as one — the load-bearing claim is now the gate")
    a("platform, not the diff.")
    a("")
    a("The budget still has not been touched, for the same reason as before:")
    a("`rig_replay_speed` is DERIVED from it, and widening a gate to make a")
    a("suite green is the thing this project does not do. Moving the gate to a")
    a("machine that meets the UNCHANGED budget is the opposite move, and it")
    a("does not settle the macOS question: whether D7's declared headroom")
    a("(\"roughly 2x the LOADED figure\") still holds on a developer laptop is")
    a("open, and it belongs to the session that owns D9's rig, before that")
    a("rig's numbers are trusted.")
    a("")

    # ---------------------------------------------------------------- 10b
    a("## 10b. Adversarial review before the D8.0 hand-back")
    a("")
    a("Five lenses over the whole D8.0 diff — silent loss, encode/decode")
    a("asymmetry, C correctness and memory, tests that cannot fail, and")
    a("scope/labels/honesty — with every candidate finding handed to an")
    a("independent skeptic whose instruction was to REFUTE it and who could")
    a("build and run the daemon to settle it.")
    a("")
    a("**29 candidate findings, 15 refuted, 14 confirmed and fixed before")
    a("hand-back.** The confirmed fourteen, because what a review caught is")
    a("more useful to the next phase than a claim that it found nothing:")
    a("")
    a("| # | Found | Fix |")
    a("| --- | --- | --- |")
    a("| 1 | A measurement datagram the socket refused was counted as SENT and "
      "reached no drop total, so `HealthPacket.drops` stayed still for the one "
      "failure that happens on the link the Jetson is watching | `send_failures` "
      "is now a fourth cause inside `total_drops`, and measurement datagrams "
      "are counted on success only |")
    a("| 2 | The portable detector abandoned the rest of a frame after 254 "
      "components with only a log line, AND it kept the first 254 in raster "
      "order — so a saturated frame went blind below a scanline and the cap's "
      "\"largest areas survive\" was silently pre-empted by scan position | the "
      "list now keeps the BEST 254 by the cap's own order (matching what the "
      "IVE hardware does, which sheds the smallest regions), everything shed "
      "is counted per frame and per run, and the count reaches `--stats` and "
      "the health drop total |")
    a("| 3 | A failed detector frame `continue`d past the housekeeping block, so "
      "a detector failing on EVERY frame produced total silence on the health "
      "plane — indistinguishable from a dead node | the failure branch now goes "
      "to housekeeping; health and control keep running |")
    a("| 4 | The control-plane frame read was bounded per-`read()` by "
      "`SO_RCVTIMEO`, not per frame, so a peer dribbling one byte at a time "
      "held the single-threaded loop indefinitely | a total 5 ms deadline for "
      "one control frame (`sw_read_exact_by`), well under a frame period |")
    a("| 5 | A partial `ive_alloc` failure orphaned the MMZ blocks it had "
      "taken and retried every frame, leaking DMA memory on a 256 MB node | "
      "rollback on failure, geometry committed only after success |")
    a("| 6 | The same defect in the portable detector: `soft_alloc` committed "
      "the geometry before its allocations, so the frame after an OOM "
      "dereferenced NULL | allocate first, commit after, free what arrived |")
    a("| 7 | Section 4's \"nanopb == host codec: yes\" and \"Verdict: EXACT\" "
      "were hardcoded literals in this generator — the report's central claim "
      "asserted, in the document whose job is to test it | both are read from "
      "`wire_identity` in the evidence, produced by running the C tool |")
    a("| 8 | The suite counts in section 10 were literals, and had drifted to "
      "FALSE as tests were added | recorded by `measure` from an actual "
      "`pytest -q` and quoted from the evidence |")
    a("| 9 | The anti-tuning gate tying the declared tolerances to this "
      "document was a whole-document substring search: 9 of 12 corrupted "
      "bounds went undetected, including a 10x-looser host bound | the test "
      "parses section 6's tables by row label and column |")
    a("| 10 | `test_cap_survivors_keep_their_offered_order` passed under the "
      "exact bug it is named for — its fixture's top-N happened to be "
      "ascending in both orders | new fixture where the two orders differ, "
      "plus an assertion that they still differ |")
    a("| 11 | A cmake CONFIGURE failure (a deleted source, a broken "
      "CMakeLists) was classified as an environment problem: 30+ tests "
      "skipped and the suite exited 0 | only a missing toolchain skips; "
      "configure and build failures fail, and both binaries must exist |")
    a("| 12 | E3's per-frame RNG key: deleting the seed, or the camera id, "
      "from it left all tests passing — every comparison was against an "
      "UNJITTERED baseline | each ingredient is now varied on its own |")
    a("| 13 | Neither artifact-producing command took a seed and no artifact "
      "recorded one, against the standing determinism rule | `--seed` on "
      "`fixtures` and on `report measure`; recorded in `stats.json` and in "
      "the evidence |")
    a("| 14 | `measure` recorded nothing identifying the binary it measured, "
      "and its docstring claimed it built one | the evidence records the "
      "daemon's SHA-256; the docstring says what it actually does |")
    a("")
    a("Findings 7-12 are TEST-STRENGTH defects: the code was right and the")
    a("tests could not have caught it being wrong. Findings 1-6 were real")
    a("daemon defects, four of them on failure paths that only open when")
    a("something else has already gone wrong — which is exactly where a node")
    a("in a field for a year gets to spend its time.")
    a("")
    a("Two of the refuted fifteen are worth naming because the refutation is")
    a("the useful part: a claimed `+25.0 px` offset in the host detector does")
    a("not exist (a test's deliberate perturbation, misattributed), and a")
    a("claimed in-contract overflow in the portable detector's label plane is")
    a("arithmetically impossible at the declared bounds.")
    a("")

    # ---------------------------------------------------------------- 10c
    a("## 10c. Adversarial review before the D8.0a hand-back")
    a("")
    a("The same shape, over the amendment's diff: five lenses — host/edge")
    a("numerical identity, honesty and labels, tests that cannot fail, scope")
    a("and blast radius, and C correctness and fixture integrity — each")
    a("candidate finding handed to an independent skeptic told to REFUTE it,")
    a("with a compiler and the suite available to settle it.")
    a("")
    a("**17 candidates, 7 refuted, 10 confirmed — 6 distinct defects, all")
    a("fixed before hand-back.** (Four filings were the same un-regenerated")
    a("report and two were the same quantifier.)")
    a("")
    a("| # | Found | Fix |")
    a("| --- | --- | --- |")
    a("| 1 | The generator was rewritten for the amendment and its ARTIFACT was "
      "not re-run, so this document went on asserting the superseded constant "
      "while the code beside it did the opposite — and `cap.py`'s own comment "
      "pointed the reader at a D8-F1 that contradicted it. The evidence file "
      "was stale too, so `generate` alone would have re-emitted PASS (84 "
      "tests) and a byte-identity verdict measured with the pre-amendment "
      "binary against the pre-regeneration fixtures | rebuild, `measure`, "
      "`generate`, both artifacts committed with the code; plus an E5 test "
      "asserting the committed report IS `generate(committed evidence)`, "
      "which is deterministic because the generator promises byte-identity "
      "for identical inputs |")
    a("| 2 | The new E5 saturation clip produced components of 70-90 px, so it "
      "proved the clamp is reachable and pinned nothing: the daemon's `50` "
      "could be set to ANY value in 41..69 with the whole suite green | the "
      "clip now STRADDLES the point (38..91 px, including 49, 50 and 51) and "
      "asserts it still does. Verified by mutation: 41, 48, 49 and 69 all now "
      "fail. 51 still passes, and correctly — at exactly 50 both expressions "
      "produce a bit-identical 1.0, so `>` versus `>=` is untestable by "
      "construction, which the test says out loud |")
    a("| 3 | The amendment silently REMOVED coverage. Confidence is injective "
      "in area below saturation, so the cap comparator's `-area_px` level "
      "stopped being the deciding one anywhere the suite looked; it decides "
      "again only when several SATURATED components compete for the cap, and "
      "nothing built that — clutter exceeds the cap without saturating, the "
      "saturation clip saturates without exceeding it | a second clip: nine "
      "fat movers against the cap of seven, all at confidence 1.0. Verified "
      "by mutation: reversing the C comparator's area level fails this test "
      "and NOTHING else in the suite |")
    a("| 4 | `sw_pipeline.c`'s stated reason for dividing in double — that "
      "dividing in float \"would land on a different bit pattern for some "
      "areas\" — is false for every area the clamp admits | reworded to say "
      "what is true: the two agree bit-for-bit over 0..49 and first diverge "
      "at 2^24+1, where it is the operand conversion that loses. The double "
      "form is kept because it mirrors the host's SHAPE, not because it "
      "changes a byte today |")
    a("| 5 | D8-F7 said the regeneration moved the field on \"all N\" "
      "observations while section 3.2 derived the saturation-aware count from "
      "the same fixtures — the literal-claim failure mode the new counter "
      "block was added to prevent, in the paragraph that introduced it | both "
      "places derive it identically now |")
    a("| 6 | `stats.json` is the one committed artifact nothing derives, and "
      "this report prints four of its numbers: a wholly falsified stats file "
      "passed the entire suite, and `gate/stats.json` was read by no test at "
      "all. The regeneration exposed it by ADDING a `seed` key — the "
      "committed files had drifted from their own writer | two clip-free "
      "tests: the committed schema must match what `write_fixture` actually "
      "writes (keys only — values are machine-specific), and the four "
      "published counters must equal what the byte-gated `observations.swob` "
      "and `packets.hex` say |")
    a("")
    a("Findings 2, 3 and 6 are TEST-STRENGTH defects and 3 is the one worth")
    a("carrying forward: an amendment can DELETE coverage without touching a")
    a("test, by making one branch of a comparator unreachable from every")
    a("fixture. Nothing goes red when that happens. It was found by mutating")
    a("the C source and re-running the suite, which is the only method that")
    a("finds it.")
    a("")
    a("Three of the seven refutations are worth naming:")
    a("")
    a("- the wire `confidence` has no tolerance axis, but it does not need")
    a("  one: `Observation2D.confidence` is `ge=0.0, le=1.0` in the frozen D0")
    a("  contract, and every board path decodes through that validator before")
    a("  any tolerance comparison is reachable, so an out-of-range confidence")
    a("  dies loudly upstream rather than being measured;")
    a("- the \"integer-safe\" rationale was filed as arithmetically false in")
    a("  full. It is not: the integer comparison is exactly equivalent to its")
    a("  float form over all 2^32, and the skeptic extended the check to every")
    a("  candidate saturation constant from 2 to 65536. Only the one sentence")
    a("  in finding 4 was wrong;")
    a("- E2's decode-direction test does not assert `conf`. True, and left")
    a("  alone: it asserts 12 of 25 fields and `conf` is not singled out, so")
    a("  this is a pre-existing breadth gap in that test rather than anything")
    a("  D8.0a introduced. Recorded here as a D9 lever, not patched in a")
    a("  phase that has no claim on it.")
    a("")

    # -------------------------------------------------------------- 10d
    a("## 10d. Adversarial review before the D8.1-prep hand-back")
    a("")
    a("The same shape as 10b and 10c, over the D8.1-prep diff: five lenses —")
    a("honesty and labels, tests that cannot fail, measurement correctness,")
    a("scope and blast radius, and correctness/robustness of the new code —")
    a("with every candidate finding handed to an independent skeptic whose")
    a("instruction was to REFUTE it and who could build and run the harness to")
    a("settle it.")
    a("")
    a("**15 candidates, 8 refuted, 7 confirmed — 6 distinct defects, all fixed")
    a("before hand-back.** The gap between 7 and 6 is one defect filed twice by")
    a("two different lenses, which is what independent lenses are for.")
    a("")
    a("| # | Found | Fix |")
    a("| --- | --- | --- |")
    a("| 1 | Section 1's status row said the D8.1-prep work was \"complete on "
      "this host\" as a literal, while section 2.1 seventy lines later said the "
      "image build was PENDING. The generator was structurally incapable of "
      "agreeing with itself — the same literal-claim failure as review 10c "
      "finding 1 | the row is derived from the image manifest, and the image "
      "build gets its OWN row: building the tooling and running it to "
      "completion are different claims |")
    a("| 2 | `Reproducibility.passes` was `not mismatches and not breaches`, so "
      "two runs in which every axis was NOT-MEASURED and every counter absent "
      "reported PASS — while section 8.1 asserted the opposite in prose, and "
      "the test NAMED for the guarantee never asserted it | the verdict is "
      "three-valued (`pass`/`fail`/`incomplete`), it rides in the artifact, "
      "the CLI no longer passes on an empty run list (`all([])` is True), and "
      "the test now makes the assertion its name promises |")
    a("| 3 | `provision.py` had no test at all, and the report already called "
      "it host-exercised. Worse, running it exposed why: `LocalTransport` "
      "translated node paths for push/fetch but not for run/spawn, so with the "
      "module's OWN default `remote_dir` the binary was pushed to one path and "
      "started from another — outside the sandbox, which on a Linux host is a "
      "real directory | `_translate` applies to run and spawn too, and "
      "`test_d81_provisioning.py` drives push, hash-verify, spawn, SIGTERM and "
      "collect against the host-built daemon, including the truncated-transfer "
      "refusal |")
    a("| 4 | The A7-utilisation collector differenced "
      "`getrusage(RUSAGE_CHILDREN)`, which accumulates over EVERY reaped "
      "child — and on a host with no `/proc` the peak-RSS sampler forks `ps` "
      "fifty times a second INSIDE that window. A skeptic isolated it: 0.08 "
      "core of \"A7 utilisation\" attributed to a daemon that did not exist | "
      "`os.wait4` returns rusage for one pid and cannot be contaminated by "
      "another; the source string now says which pid it asked about |")
    a("| 5 | A daemon that was killed or died wrote no `--stats` file, and "
      "`frames_in` read the absence as 0, which became a MEASURED \"0.00 fps\" "
      "for a run that never happened | `Run.frames_in` is `int | None` and "
      "`sustained_fps` returns NOT-MEASURED with the reason; unknown is not "
      "zero |")
    a("| 6 | The 8.1 anti-tuning gate was circular: it compared the report's "
      "table against the same constant the generator renders that table from, "
      "so it could only ever prove the document had been rebuilt. Section 6's "
      "gate has had the same shape since D8.0 | the three numbers are now "
      "pinned as LITERALS in the test as well, so widening a declared bound "
      "has to be done deliberately, in a place a reviewer reads |")
    a("")
    a("Two more defects were found by running the work rather than reading it,")
    a("and they are the argument for the amendment's \"exercised against the")
    a("HOST-built daemon\" clause:")
    a("")
    a("- the health listener caught only `WireError` around `decode_health`,")
    a("  which does not cover protobuf's `DecodeError` — one corrupt datagram")
    a("  killed the monitor. Found by the corrupt-datagram test on its first")
    a("  run; finding D8-F10 records that the D7 ingest adapter still has it;")
    a("- the daemon's output went to a PIPE that this harness only drained")
    a("  after the process exited. A soak logging a warning per frame would")
    a("  have filled the pipe buffer and deadlocked — hours in, silently. It")
    a("  went to a file as part of the `wait4` fix above, which is the sort of")
    a("  thing a fix for one defect is allowed to be.")
    a("")
    a("Two test-strength defects, both in tests written by this work:")
    a("")
    a("- `test_a_listener_on_the_wrong_port_hears_nothing` was near-vacuous: no")
    a("  datagram is addressed to an unadvertised ephemeral port under ANY")
    a("  implementation, including one with a separate health port. Replaced")
    a("  with the claim that has content — one socket receives BOTH payload")
    a("  types, plus an assertion that the daemon's `--help` has no health")
    a("  port to move to;")
    a("- the two-run E8 test enforced the BOARD's 10% fps bound on this host")
    a("  and went red at 17% while the emulated image build was using four")
    a("  cores. Widening the bound would have been gate-weakening; the test now")
    a("  gates what a host can honestly gate — the deterministic counters are")
    a("  identical and the axes get compared — and the bound is judged on the")
    a("  board, where it applies. This is the W6 host-load lesson again, in a")
    a("  new place.")
    a("")
    a("Three refutations worth naming, because the refutation is the useful")
    a("part:")
    a("")
    a("- a claimed sandbox escape in `LocalTransport.alive` (\"the zombie reads")
    a("  as alive, so `stop_daemon` always returns False\") was real when it was")
    a("  filed and had already been fixed by the time the skeptic ran it — the")
    a("  skeptic showed `Popen.poll()` reaps, which is exactly what the fix")
    a("  added;")
    a("- \"the E8 anti-tuning gate is circular: it reads a table the generator")
    a("  writes from the same constant\". True, and it is the same circularity")
    a("  section 6's gate has had since D8.0. What either gate actually catches")
    a("  is a HAND-EDITED report, which is also what the freshness test exists")
    a("  for. Recorded rather than papered over;")
    a("- \"two new unsanctioned `pytest.skip`s\" — both are the skip E5 already")
    a("  uses for a missing `D8_EDGE_REPORT.md`, which is a committed repo file")
    a("  rather than an environment dependency. Consistent with the precedent,")
    a("  and it never fires in a checkout that has the report.")
    a("")

    # ---------------------------------------------------------------- 10e
    a("## 10e. Adversarial review before the D8.1 Phase A hand-back")
    a("")
    a("The same shape as 10b, 10c and 10d, over the runbook's Phase A diff:")
    a("five lenses — honesty and labels, tests that cannot fail, C correctness/")
    a("memory/refusals, measurement correctness, and scope/blast radius against")
    a("the runbook — with every candidate handed to an independent skeptic whose")
    a("instruction was to REFUTE it and who could build and run the daemon and")
    a("the harness to settle it.")
    a("")
    a("**42 candidates, 20 refuted, 22 confirmed — 17 distinct defects, all")
    a("fixed or recorded before hand-back.** The gap between 22 and 17 is five")
    a("defects filed twice by two different lenses, which is what independent")
    a("lenses are for: the RAM-budget arithmetic was found by both honesty and")
    a("measurement, from opposite ends.")
    a("")
    a("| # | Found | Fix |")
    a("| --- | --- | --- |")
    a("| 1 | **The harness derived a clip length its own daemon refuses.** "
      "`main.c` enforces `clip + detector + fixed`, and `fixed` includes "
      "`inject.luma_capacity` — a full frame, malloc'd on every injection open "
      "— plus five struct terms. The harness subtracted only the detector. At "
      "2304x1296 and 1536x864 the omission is smaller than the headroom and the "
      "run happens to work; at 1152x648 the published row left 249,856 B "
      "against a luma term of 746,496 B, so the daemon exits 1 with \"RAM "
      "budget exceeded\" — at the resolution most likely to sustain 30 fps and "
      "be Chosen by C4. Reproduced through `run_sweep` itself | "
      "`daemon_fixed_bytes()` is now a term in the harness's own arithmetic, in "
      "`ram_loop_max_frames`, in the published table and in every run record. "
      "The derived IVE length at 1152x648 moves 174 -> 171 |")
    a("| 2 | The test that should have caught #1 compared "
      "`ram_loop_max_frames()` against `ram_budget_row()` — the same arithmetic "
      "on both sides of the assertion, so no divergence between harness and "
      "daemon could ever redden it | Replaced by two tests that read the "
      "DAEMON: one parses the daemon's own printed `fixed` and asserts the "
      "declared allowance bounds it, one runs the derived clip and the old "
      "budget-minus-detector clip and asserts the first is accepted and the "
      "second refused |")
    a("| 3 | `--ram-loop-frames -1` parsed to 4294967295 and ran; "
      "`4294967298` truncated to 2; `200,000` became 200; "
      "`--ram-loop-period-ns 33.3e6` became 33 ns and paced nothing | "
      "Checked `parse_u32_arg`/`parse_i64_arg` on all three numeric RAM flags: "
      "whole string consumed, `errno` checked, sign rejected, range checked. "
      "Refuses, never clamps |")
    a("| 4 | `peak_rss_mb` divided KiB by 1024, i.e. binary MiB wearing a "
      "decimal-MB label, in the one phase whose entire memory argument is "
      "decimal MB and whose budget it would be read against | Converted to "
      "decimal; the collector registry states both sources are KiB. The axis "
      "had no test at all, which is why it survived |")
    a("| 5 | `scene_looped: True` was asserted on every soak record, including "
      "soaks whose scene never wrapped, and the RAM-loop scene note travelled "
      "with it — a declared systematic attached to runs that did not have it | "
      "Derived per run from the clip and the served count |")
    a("| 6 | `BenchmarkPlan.ram_paced_fps` was written into the scored artifact "
      "and read by nothing | Deleted, with a comment naming the three fields "
      "that do state the applied pace |")
    a("| 7 | `compare_runs` never checked that its two runs were the same "
      "CONFIGURATION, so two different resolutions — or a peak RSS read by "
      "`ps` on one side and procfs on the other — compared as a clean pass | "
      "`config_mismatches` on label, proc grid, pace, RAM plan and per-axis "
      "measurement source; any mismatch forces `fail` |")
    a("| 8 | This report claimed the whole-suite row came from the Linux gate "
      "platform as flat prose, sitting under a table that can read PENDING or "
      "Darwin and a row that can read one failure | Derived, in four states, "
      "from the recorded platform AND the recorded receive buffer: a Linux box "
      "under `GATE_RMEM_MAX_BYTES` does not get to claim the gate either |")
    a("| 9 | Section 2.1 narrated an abandoned emulated-Mac attempt directly "
      "above a manifest table from a different, completed build, and nothing "
      "recorded which machine produced the hashes | The mechanical lesson is "
      "kept and retitled by what it explains, in the past tense; every status "
      "claim is derived from the manifest; a Build host row renders "
      "NOT-MEASURED with its reason (finding D8-F14) |")
    a("| 10 | `detector_state_bytes(..., \"ive\")` claimed to mirror the C "
      "exactly and to be pinned equal by a test; it omits `sizeof(IVE_CCBLOB_S)` "
      "and no test compares it to anything | The claim is scoped per arm and "
      "the exclusion declared: every IVE cell is stated as a LOWER bound on the "
      "board's sum. The arithmetic was NOT changed — the type is an SDK "
      "header's and guessing its size would put an uncontradictable number in a "
      "byte-precision published total |")
    a("| 11 | The E8 \"never folded into any bound\" assertions could not fail: "
      "they compared a note constant to itself | Replaced by a test that "
      "asserts the actual property — the bounded axes are exactly the three "
      "declared ones, and no note constant is among them |")
    a("| 12 | E7's paced-health test claimed to gate where the pace sleep sits. "
      "Mutation showed the defect it named leaves the measured maximum health "
      "period indistinguishable from baseline | Claim corrected to what the "
      "test does gate, and the declared slack TIGHTENED 3.0 -> 2.0, derived "
      "from the arithmetic of a late packet vs a missed one rather than tuned |")
    a("| 13 | Two more claims asserted over the row they describe: \"the run "
      "recorded above is a clean one\" (W6) and D8-F8's two-term description of "
      "the budget check, stale once harness and daemon were reconciled on three "
      "| Both derived |")
    a("| 14 | `test_the_preload_stays_out_of_the_daemons_fps_denominator` "
      "cannot fail for the defect it names: both rates share `frames_in` as a "
      "numerator, so the daemon's interval is structurally a subset of the "
      "harness's whatever the preload does | Renamed and the false causal claim "
      "struck. Gating it properly needs an observable that does not exist — see "
      "the gaps below |")
    a("| 15 | The gate-platform publish test used byte-identical fixture values "
      "for both receive-buffer knobs, so a dropped or duplicated row was "
      "invisible | Distinguishable values, both still above the A1 line |")
    a("| 16 | The RAM-loop budget arithmetic pin asserted `(w + 15) & ~15 == w` "
      "— true of the literals, about no system under test | Replaced with the "
      "three-term arithmetic and an explicit assertion that the pre-fix "
      "174-frame row goes over on the luma term alone |")
    a("| 17 | A false claim in live source: `main.c`'s pace block said a sleep "
      "ahead of the health check would delay every health packet, which the "
      "review measured to be untrue | Comment corrected, and it now names the "
      "property that IS gated — byte-identical packet logs paced vs unpaced |")
    a("")
    a("**Three defects were found by running the work rather than reading it,**")
    a("and all three are the argument for the reproduction requirement: #1 (the")
    a("skeptic drove `run_sweep` at the published row and read the refusal), #3")
    a("(every bad argument was fed to the built binary), and #12 (the mutation")
    a("was built and measured four times against a baseline, which is the only")
    a("reason the docstring's claim was known to be false rather than merely")
    a("doubted).")
    a("")
    a("**Test-strength defects — #2, #11, #14, #15, #16 — are five of")
    a("seventeen,** and #2 is the one worth naming twice: it did not merely fail")
    a("to catch #1, it was structurally incapable of catching it, because both")
    a("sides of its assertion came from the same function's arithmetic. A test")
    a("whose two sides share a source is a tautology wearing a comparison.")
    a("")
    a("Two gaps are recorded rather than closed, because closing them is more")
    a("than this phase is sanctioned to do:")
    a("")
    a("- **the preload has no observable.** `write_stats` emits no preload")
    a("  timing, so nothing can gate that the clip loads before the daemon's own")
    a("  clock starts. One key (`ram_preload_ns`) would fix it; it is a wire-")
    a("  adjacent addition to a stats file the board session reads, and it")
    a("  belongs to the session that owns C3;")
    a("- **the IVE arm of every byte figure here is uncompiled.** "
      "`sw_detect_ive.c` builds as a refusal without `SKYWEAVE_HAVE_RKMPI`, so")
    a("  `ive_footprint_for` — the code that produces the 119,439,360 B at")
    a("  2304x1296 on which D8-F11 and the whole clip-length question rest — has")
    a("  never been compiled, let alone run. The first RAM-loop start on node 1")
    a("  is the moment that arithmetic is confirmed, and the INFO line it prints")
    a("  is where to read it.")
    a("")
    a("Refutations worth naming, because the refutation is the useful part:")
    a("")
    a("- \"the RAM-loop sweep measures a detector that has gone blind\" — the")
    a("  absorption effect is real and is already declared in")
    a("  `RAM_LOOP_SCENE_NOTE`, but the skeptic established the sweep is 120")
    a("  frames, far inside the model's time constant, so the impact story does")
    a("  not hold at C3's scale. It holds at C5's, and that is stated where the")
    a("  soak is described rather than as a defect here;")
    a("- \"A3 was sanctioned as catch `DecodeError`; the code catches bare")
    a("  `Exception`\" — refuted from the finding's own text, which prescribes")
    a("  \"the measurement branch's own shape, a broad catch with a labelled")
    a("  reject\". The broad catch IS the sanctioned shape;")
    a("- \"Phase A ships a replacement for a Chosen D0 decision while filing")
    a("  D8-F11 saying that number belongs to the planning session\" — refuted:")
    a("  the daemon refuses the sanctioned length, so SOME length had to be")
    a("  derived to have a working source at all. The derived value is labelled")
    a("  derived arithmetic, the D0 entry is untouched, and the finding asks for")
    a("  the amendment rather than assuming it.")
    a("")

    # ---------------------------------------------------------------- 11
    a("## 11. Out of scope, confirmed untouched")
    a("")
    a("No engine, threshold or fusion change. No NPU classifier. No RTSP/VENC")
    a("beyond the debug smoke look. No real CSI/PTS characterisation — C2 and")
    a("C3 stay parked, and the VI capture path is present, compiled, and")
    a("declared a smoke test with no PTS claim. `v1/` untouched. The bench")
    a("session (conversion-gain PTC, CFA check, sky footage) lands in")
    a("`D3_SENSOR_NOTES.md`, not here.")
    a("")
    a("Two decisions were implemented and both were written down before the")
    a("code moved: the capacity touch, exactly as the D0 \"D8 opening\" entry")
    a("authorised it, and the wire confidence, exactly as the \"D8.0")
    a("amendment\" entry authorised it. Nothing under")
    a("`v2/src/skyweave2/contracts/` changed for either — the amendment moves a")
    a("VALUE the detector puts in an existing field, not the field, its type or")
    a("its bounds — and the fixture regeneration it required is D8 fixtures")
    a("only, with `golden/` untouched.")
    a("")
    a("**Phase A of the D8.1 runbook re-derives this list, because it touched")
    a("more than the D8.1-prep diff did.** What it changed: `transport/`, one")
    a("`except` clause on `SocketIngestAdapter.poll`'s health branch (A3, the")
    a("sanctioned F10 fix); `firmware/rv1106/`, a fourth source mode and the")
    a("budget check it executes (A4, sanctioned by the D0 \"D8.1 opening\" F8")
    a("row); `skyweave2/edge/`, the harness that declares and records it, plus")
    a("the evidence file's new gate-platform block (A1); this generator; and")
    a("`firmware/rv1106/image/image-manifest.json`, which is a build product")
    a("checked in on purpose (A2).")
    a("")
    a("What it did NOT change, stated as a checkable claim rather than an")
    a("intention: nothing under `v1/`, `golden/`, `v2/proto/`,")
    a("`firmware/rv1106/proto/` or `v2/src/skyweave2/contracts/`; no committed")
    a("byte fixture under `tests/edge/fixtures/`; no wire message, no health")
    a("field (the RAM loop's source mode and byte rate reach the `--stats` JSON")
    a("and the run record, never the frozen `HealthPacket`); no declared bound")
    a("in `tolerance.py`; no existing test deleted or weakened. The three")
    a("decisions-log entries Phase A raises are DRAFTED for the planning")
    a("session and are not written here — the runbook reserves that file, and")
    a("D8-F11 is the one that needs answering before Phase B.")
    a("")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="D8 edge report")
    sub = parser.add_subparsers(dest="command", required=True)

    measure_parser = sub.add_parser("measure", help="replay the fixtures, record evidence")
    measure_parser.add_argument("--out", default=str(DEFAULT_EVIDENCE))
    measure_parser.add_argument("--work", default=None)
    measure_parser.add_argument("--build-dir", default=None)
    # Explicit, per the standing determinism rule.
    measure_parser.add_argument("--seed", type=int, default=fixtures.SCENE_SEED)
    measure_parser.add_argument(
        "--no-suites", action="store_true",
        help="skip the pytest runs (the report's suite rows then read PENDING)",
    )

    generate_parser = sub.add_parser("generate", help="write the report")
    generate_parser.add_argument("--evidence", default=str(DEFAULT_EVIDENCE))
    generate_parser.add_argument("--out", default=str(DEFAULT_REPORT))

    args = parser.parse_args(argv)

    if args.command == "measure":
        import tempfile

        work = Path(args.work) if args.work else Path(tempfile.mkdtemp(prefix="d8-"))
        build_dir = Path(args.build_dir) if args.build_dir else None
        evidence = measure(work, build_dir=build_dir, seed=args.seed,
                           run_suites=not args.no_suites)
        Path(args.out).write_text(
            json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"wrote {args.out}")
        return

    evidence_path = Path(args.evidence)
    evidence = json.loads(evidence_path.read_text()) if evidence_path.exists() else None
    if evidence is None:
        print(f"no evidence at {evidence_path}: measured rows will read PENDING")
    Path(args.out).write_text(generate(evidence), encoding="utf-8")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()


__all__ = ["asdict", "generate", "measure"]
