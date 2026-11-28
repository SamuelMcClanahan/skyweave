#!/usr/bin/env python3
"""Check the SkyWeave FC parameter baseline (contract B-D4).

Three checks, each a subcommand. None of them writes a parameter to a
flight controller, and none of them talks to hardware.

``lint`` (no network, no SITL; standard library only)
    The file parses as ``NAME,VALUE`` lines, has no duplicate, and holds the
    project rules listed in :data:`RULES` (each rule names its contract
    clause). It also refuses names that are calibration or tuning results.

``meta`` (needs the ArduPilot metadata file; ``--download`` needs the network)
    Every name exists in ArduPilot's published parameter metadata for the
    Copter stable release (``apm.pdef.xml``), and every value is inside the
    documented range, is a documented enumeration value, or sets only
    documented bitmask bits. The metadata file lives in ``<repo>/_local/``
    (git-excluded) and is never committed.

``sitl`` (needs Linux x86-64: run it through ``v2/docker/sitl/run.sh``)
    Boots the pinned ArduCopter SITL with the baseline as one more defaults
    file and reads every parameter back over MAVLink. It reports the names
    SITL does not have (hardware-only parameters).

Usage, from the repo root::

    python3 v2/deploy/fc/check_params.py lint
    python3 v2/deploy/fc/check_params.py meta --download
    v2/docker/sitl/run.sh shell -c "python deploy/fc/check_params.py sitl"

``lint`` and ``meta`` exit 0 only when nothing fails. ``sitl`` exits 0 only
when every parameter SITL has reads back with the file's value. The script
is not part of the pytest tiers: ``meta --download`` needs the network and
``sitl`` needs the SITL platform.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import struct
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
DEFAULT_PARAM_FILE = HERE / "skyweave_cone_x.param"

DEFAULT_RELEASE = "stable-4.7.0"
PDEF_URL = "https://autotest.ardupilot.org/Parameters/versioned/Copter/{release}/apm.pdef.xml"
PDEF_SHA256 = {
    # Recorded on 2026-11-27. A different hash is reported, not refused: the
    # file is data for a comparison, never executed.
    "stable-4.7.0": "d0b75b4e081cfb13f1f109222834347104073c11a8c0cc372a5b4e981d1d4107",
}

NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,15}$")  # ArduPilot names: at most 16 characters


class ParamFileError(ValueError):
    """The parameter file does not parse."""


# --------------------------------------------------------------------------
# The parameter file
# --------------------------------------------------------------------------


def parse_param_file(path: Path) -> dict[str, float]:
    """``NAME,VALUE`` lines; ``#`` starts a comment line; blank lines allowed.

    This is the format Mission Planner and MAVProxy load and the format an
    ArduPilot defaults file accepts. A duplicate name, a bad name or a value
    that is not a finite number is an error.
    """
    out: dict[str, float] = {}
    for n, raw in enumerate(path.read_text(encoding="ascii").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(",")
        if len(parts) != 2:
            raise ParamFileError(f"{path.name}:{n}: expected NAME,VALUE, got {raw!r}")
        name, text = parts[0].strip(), parts[1].strip()
        if not NAME_RE.match(name):
            raise ParamFileError(f"{path.name}:{n}: bad parameter name {name!r}")
        if name in out:
            raise ParamFileError(f"{path.name}:{n}: duplicate parameter {name}")
        try:
            value = float(text)
        except ValueError:
            raise ParamFileError(
                f"{path.name}:{n}: {name} value {text!r} is not a number"
            ) from None
        if value != value or value in (float("inf"), float("-inf")):
            raise ParamFileError(f"{path.name}:{n}: {name} value {text!r} is not finite")
        out[name] = value
    if not out:
        raise ParamFileError(f"{path.name}: no parameters")
    return out


# --------------------------------------------------------------------------
# lint: project rules
# --------------------------------------------------------------------------

FORBIDDEN_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"^SIM_", "a simulator parameter"),
    (r"^COMPASS_(OFS|DIA|ODI|MOT|SCALE|DEV_ID|PRIO)", "a compass calibration result"),
    (r"^COMPASS_DEC$", "a site value (declination)"),
    (r"^INS_(ACC|GYR)\d?(OFFS|SCAL)", "an IMU calibration result"),
    (r"^INS_(ACC|GYR)\d?_ID$", "an IMU identity"),
    (r"^INS_(ACC|GYR)\d?_CALTEMP$", "an IMU calibration result"),
    (r"^INS_TCAL", "an IMU temperature calibration result"),
    (r"^AHRS_TRIM", "a level calibration result"),
    (r"^RC\d+_(MIN|MAX|TRIM|REVERSED)$", "a radio calibration result"),
    (r"^ATC_RAT_(RLL|PIT|YAW)_(P|I|D|FF|D_FF|IMAX)$", "a PID tuning result"),
    (r"^ATC_ANG_(RLL|PIT|YAW)_P$", "a PID tuning result"),
    (r"^INS_HNTCH_(FREQ|BW|REF|MODE|HMNCS|OPTS|ATT|FM_RAT)$", "a notch filter setting"),
    (r"^INS_HNTC2_", "a notch filter setting"),
    (r"^BARO\d?_GND_", "a barometer ground value"),
    (r"^STAT_", "a statistic"),
    (r"^SERVO_BLH_TEST$", "a motor test switch"),
    (r"^SYSID_ENFORCE$|^MAV_OPTIONS$", "a MAVLink source filter"),
)
"""Names the baseline must not carry (README, "Not set here on purpose")."""

ALT_HOLD, GUIDED, RTL = 2.0, 4.0, 6.0


def _flight_mode_slot(pwm: int) -> int:
    """ArduCopter's six flight-mode slots by PWM (the mode-switch thresholds)."""
    for slot, top in enumerate((1230, 1360, 1490, 1620, 1749), start=1):
        if pwm <= top:
            return slot
    return 6


def _eq(name: str, want: float) -> Callable[[dict[str, float]], str | None]:
    def rule(p: dict[str, float]) -> str | None:
        if name not in p:
            return f"{name} is not in the file"
        return None if p[name] == want else f"{name} is {p[name]:g}, expected {want:g}"

    return rule


def _switch_modes(p: dict[str, float]) -> str | None:
    got = [p.get(f"FLTMODE{_flight_mode_slot(pwm)}") for pwm in (1000, 1500, 2000)]
    if got != [ALT_HOLD, GUIDED, RTL]:
        return f"1000/1500/2000 us give modes {got}, expected ALT_HOLD(2)/GUIDED(4)/RTL(6)"
    guided = [s for s in range(1, 7) if p.get(f"FLTMODE{s}") == GUIDED]
    if guided != [4]:
        return f"GUIDED is on slots {guided}; only the centre slot 4 may be GUIDED"
    return None


def _mode_channel(p: dict[str, float]) -> str | None:
    ch = p.get("FLTMODE_CH")
    if ch is None or not 5 <= ch <= 15:
        return f"FLTMODE_CH is {ch}; a mode channel is needed"
    if p.get(f"RC{int(ch)}_OPTION") != 0:
        return f"RC{int(ch)}_OPTION must be 0: the mode channel carries no other function"
    return None


def _estop(p: dict[str, float]) -> str | None:
    chans = [k for k, v in p.items() if re.match(r"^RC\d+_OPTION$", k) and v == 31]
    if len(chans) != 1:
        return f"exactly one channel must be Motor Emergency Stop (31), got {chans}"
    if chans[0] in ("RC8_OPTION", f"RC{int(p.get('FLTMODE_CH', 0))}_OPTION"):
        return f"{chans[0]} cannot be the emergency stop"
    return None


def _no_auto_arm(p: dict[str, float]) -> str | None:
    arming = {41: "ArmDisarm", 153: "ArmDisarm", 154: "ArmDisarm AirMode", 165: "Arm/EStop"}
    bad = [k for k, v in p.items() if re.match(r"^RC\d+_OPTION$", k) and int(v) in arming]
    if bad:
        return f"{bad} would arm from a switch; the baseline has no arm switch"
    if int(p.get("GUID_OPTIONS", 0)) & 1:
        return "GUID_OPTIONS bit 0 lets the transmitter arm in GUIDED"
    return None


def _spin_order(p: dict[str, float]) -> str | None:
    arm, mn = p.get("MOT_SPIN_ARM"), p.get("MOT_SPIN_MIN")
    if arm is None or mn is None:
        return "MOT_SPIN_ARM and MOT_SPIN_MIN must both be in the file"
    return None if arm <= mn else f"MOT_SPIN_ARM {arm:g} is above MOT_SPIN_MIN {mn:g}"


def _batt_order(p: dict[str, float]) -> str | None:
    keys = (
        "MOT_BAT_VOLT_MIN",
        "BATT_CRT_VOLT",
        "BATT_LOW_VOLT",
        "BATT_ARM_VOLT",
        "MOT_BAT_VOLT_MAX",
    )
    vals = [p.get(k) for k in keys]
    if any(v is None for v in vals):
        return f"need all of {keys}"
    if not all(a < b for a, b in zip(vals, vals[1:], strict=False)):
        return f"expected {' < '.join(keys)}, got {vals}"
    crt, low, cap = p.get("BATT_CRT_MAH"), p.get("BATT_LOW_MAH"), p.get("BATT_CAPACITY")
    if None in (crt, low, cap) or not 0 < crt < low < cap:  # type: ignore[operator]
        return f"expected 0 < BATT_CRT_MAH < BATT_LOW_MAH < BATT_CAPACITY, got {crt}, {low}, {cap}"
    return None


def _fence(p: dict[str, float]) -> str | None:
    if p.get("FENCE_ENABLE") != 1 or int(p.get("FENCE_TYPE", 0)) & 3 != 3:
        return "the fence must be on with the altitude and circle types"
    if not p.get("FENCE_RADIUS", 0) > 60:
        return "FENCE_RADIUS must be above the mission's 60 m geofence (contract §9)"
    if not p.get("RTL_ALT_M", 1e9) < p.get("FENCE_ALT_MAX", 0):
        return "RTL_ALT_M must be below FENCE_ALT_MAX"
    return None


def _guided_timeout(p: dict[str, float]) -> str | None:
    if not 0.1 <= p.get("GUID_TIMEOUT", 0) <= 3.0:
        return "GUID_TIMEOUT must be in [0.1, 3] s (E1-F21: 3 s is the tested value)"
    return None


def _log_bits(p: dict[str, float]) -> str | None:
    v = int(p.get("LOG_BITMASK", 0))
    return None if v & 1 and v & (1 << 12) else "LOG_BITMASK needs Fast Attitude (0) and PID (12)"


def _forbidden(p: dict[str, float]) -> str | None:
    bad = [
        f"{name} ({why})"
        for name in sorted(p)
        for pattern, why in FORBIDDEN_PATTERNS
        if re.search(pattern, name)
    ]
    return None if not bad else "must not be in the baseline: " + ", ".join(bad)


RULES: tuple[tuple[str, str, Callable[[dict[str, float]], str | None]], ...] = (
    ("R01", "X quad: FRAME_CLASS 1 (V-F1)", _eq("FRAME_CLASS", 1)),
    ("R02", "Companion on SERIAL1 is MAVLink2 ([F1], brief §2)", _eq("SERIAL1_PROTOCOL", 2)),
    ("R03", "Companion baud 921600 (brief §2)", _eq("SERIAL1_BAUD", 921)),
    ("R04", "Switch gives ALT_HOLD / GUIDED / RTL; GUIDED only at centre (E1-D16)", _switch_modes),
    ("R05", "The mode channel has no RC option", _mode_channel),
    ("R06", "Approve channel RC 8 is a plain channel ([F7], §9)", _eq("RC8_OPTION", 0)),
    ("R07", "One guarded motor emergency stop (brief 3.11)", _estop),
    ("R08", "No arm switch; no transmitter arming in GUIDED (E1-D4)", _no_auto_arm),
    ("R09", "Arming checks all on", _eq("ARMING_SKIPCHK", 0)),
    ("R10", "Radio failsafe is RTL (V-D13)", _eq("FS_THR_ENABLE", 1)),
    ("R11", "A failsafe is never ignored in GUIDED", _eq("FS_OPTIONS", 0)),
    ("R12", "Fence on, outside the mission geofence; RTL below the ceiling", _fence),
    ("R13", "GUID_TIMEOUT is not above the tested 3 s (E1-F21)", _guided_timeout),
    ("R14", "Battery thresholds are ordered", _batt_order),
    ("R15", "MOT_SPIN_ARM <= MOT_SPIN_MIN", _spin_order),
    ("R16", "Harmonic notch stays off until it is set up from a log", _eq("INS_HNTCH_ENABLE", 0)),
    ("R17", "Logging has fast attitude and PID", _log_bits),
    ("R18", "No calibration, tuning, site or simulator value", _forbidden),
)
"""Project rules. A rule returns None (holds) or the reason it fails."""


def run_lint(params: dict[str, float]) -> int:
    failures = 0
    for rule_id, text, fn in RULES:
        problem = fn(params)
        print(f"  {'ok  ' if problem is None else 'FAIL'} {rule_id} {text}")
        if problem is not None:
            failures += 1
            print(f"       {problem}")
    print(f"lint: {len(params)} parameters, {len(RULES)} rules, {failures} failed")
    return 1 if failures else 0


# --------------------------------------------------------------------------
# meta: ArduPilot's published parameter metadata
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ParamMeta:
    name: str
    human: str
    rng: tuple[float, float] | None
    values: tuple[float, ...]
    bits: tuple[int, ...]
    read_only: bool


def default_pdef_path(release: str) -> Path:
    return REPO / "_local" / "ardupilot-pdef" / f"Copter-{release}" / "apm.pdef.xml"


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def download_pdef(release: str, dest: Path) -> None:
    url = PDEF_URL.format(release=release)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".download")
    req = urllib.request.Request(url, headers={"User-Agent": "param-check"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        tmp.write_bytes(resp.read())
    tmp.replace(dest)
    print(f"meta: downloaded {url}")


def load_pdef(path: Path) -> dict[str, ParamMeta]:
    """Parse ``apm.pdef.xml``. Vehicle parameters are named ``ArduCopter:NAME``;
    library parameters carry the bare name."""
    out: dict[str, ParamMeta] = {}
    for el in ET.parse(path).getroot().iter("param"):
        name = (el.get("name") or "").split(":", 1)[-1]
        fields = {f.get("name"): (f.text or "") for f in el.findall("field")}
        rng = None
        if "Range" in fields:
            lo, hi = fields["Range"].split()
            rng = (float(lo), float(hi))
        values = []
        for v in el.iter("value"):
            try:
                values.append(float(v.get("code") or ""))
            except ValueError:
                continue
        bits = tuple(int(b.get("code") or "-1") for b in el.iter("bit"))
        out[name] = ParamMeta(
            name=name,
            human=el.get("humanName") or "",
            rng=rng,
            values=tuple(values),
            bits=bits,
            read_only=fields.get("ReadOnly", "").lower() == "true",
        )
    return out


def check_value(meta: ParamMeta, value: float) -> tuple[str, str | None]:
    """``(kind, problem)``: which documented constraint applied, and why it
    fails (None when it holds). A range wins over an enumeration: ArduPilot
    lists example values beside a range for several parameters."""
    if meta.read_only:
        return "read-only", "the parameter is read-only"
    if meta.rng is not None:
        lo, hi = meta.rng
        ok = lo <= value <= hi
        return (
            "range",
            None if ok else f"{value:g} is outside the documented range [{lo:g}, {hi:g}]",
        )
    if meta.bits:
        if value != int(value) or value < 0:
            return "bitmask", f"{value:g} is not a bitmask"
        unknown = [b for b in range(32) if int(value) >> b & 1 and b not in meta.bits]
        return "bitmask", None if not unknown else f"{int(value)} sets undocumented bits {unknown}"
    if meta.values:
        ok = value in meta.values
        return "enum", None if ok else f"{value:g} is not a documented value"
    return "unbounded", None


def run_meta(params: dict[str, float], pdef: Path, release: str) -> int:
    got = sha256_of(pdef)
    want = PDEF_SHA256.get(release)
    note = "" if want is None else (" (matches the recorded hash)" if got == want else "")
    print(f"meta: {pdef}\nmeta: sha256 {got}{note}")
    if want is not None and got != want:
        print(f"meta: WARNING: recorded hash for {release} is {want}; the published file changed")
    meta = load_pdef(pdef)
    print(f"meta: {len(meta)} parameters documented for Copter {release}")
    failures = 0
    kinds: dict[str, list[str]] = {}
    for name, value in params.items():
        if name not in meta:
            failures += 1
            print(f"  FAIL {name}: not a Copter {release} parameter")
            continue
        kind, problem = check_value(meta[name], value)
        kinds.setdefault(kind, []).append(name)
        if problem is not None:
            failures += 1
            print(f"  FAIL {name}: {problem}")
    for kind in ("range", "enum", "bitmask", "unbounded"):
        names = kinds.get(kind, [])
        print(f"meta: {len(names):3d} checked as {kind}")
    if kinds.get("unbounded"):
        print("meta: no documented range, values or bits (name checked only):")
        print("      " + ", ".join(sorted(kinds["unbounded"])))
    print(f"meta: {len(params)} parameters, {failures} failed")
    return 1 if failures else 0


# --------------------------------------------------------------------------
# sitl: read the values back from the pinned SITL
# --------------------------------------------------------------------------


def _f32(value: float) -> float:
    return struct.unpack("<f", struct.pack("<f", value))[0]


def run_sitl(params: dict[str, float], param_file: Path, dump: Path | None) -> int:
    # Imported here: lint and meta need only the standard library. The
    # MAVLink encoder is the pilot link's own (pymavlink, ardupilotmega v2).
    from skyweave2.drone.sitl import SitlInstance, ensure_sitl, free_instance

    class BaselineSitl(SitlInstance):
        """SITL with the baseline as a third defaults file, after copter.parm
        and the companion stream file. A defaults file is not a parameter
        write ([F1])."""

        def prepare(self) -> list[str]:
            argv = super().prepare()
            assert self.workdir is not None
            copy = self.workdir / param_file.name
            copy.write_bytes(param_file.read_bytes())
            i = argv.index("--defaults") + 1
            argv[i] = f"{argv[i]},{copy}"
            return argv

    sitl = BaselineSitl(ensure_sitl(), instance=free_instance())
    values: dict[str, float] = {}
    count = 0
    try:
        pilot = sitl.start(timeout_s=60.0)

        def take() -> None:
            nonlocal count
            for msg in pilot.poll():
                if msg.get_type() == "PARAM_VALUE":
                    values[msg.param_id] = float(msg.param_value)
                    count = max(count, int(msg.param_count))

        # The whole table first, then by name whatever the stream dropped.
        # With one request and no heartbeat the stream stopped at 32 of 1369
        # (2026-11-27). With a 1 Hz GCS heartbeat and a repeated request (at
        # most 4) it completes. Which of the two fixed it was not isolated.
        request = pilot._mav.param_request_list_encode(pilot.fc_sysid, pilot.fc_compid)
        pilot.send(request)
        deadline = time.monotonic() + 180.0
        last_n, last_t, last_hb, requests = -1, time.monotonic(), time.monotonic(), 1
        while time.monotonic() < deadline:
            take()
            now = time.monotonic()
            if count and len(values) >= count:
                break
            if now - last_hb >= 1.0:
                pilot.heartbeat()
                last_hb = now
            if len(values) != last_n:
                last_n, last_t = len(values), now
            elif now - last_t > 5.0:
                if requests >= 4:
                    break
                pilot.send(request)
                requests += 1
                last_t = now
            time.sleep(0.01)
        table_complete = bool(count) and len(values) >= count
        for name in params:
            for _ in range(3):
                if name in values:
                    break
                pilot.send(
                    pilot._mav.param_request_read_encode(
                        pilot.fc_sysid, pilot.fc_compid, name.encode("ascii"), -1
                    )
                )
                t_end = time.monotonic() + 1.5
                while name not in values and time.monotonic() < t_end:
                    take()
                    time.sleep(0.01)
        log = sitl.log_text()
    finally:
        sitl.stop()

    print(f"sitl: read {len(values)} of {count} SITL parameters (table complete: {table_complete})")
    if dump is not None:
        dump.parent.mkdir(parents=True, exist_ok=True)
        dump.write_text("".join(f"{k},{values[k]:.9g}\n" for k in sorted(values)))
        print(f"sitl: wrote {dump}")
    missing = sorted(n for n in params if n not in values)
    wrong = []
    for name, want in params.items():
        if name in values and abs(values[name] - _f32(want)) > 1e-6 * max(1.0, abs(want)):
            wrong.append((name, want, values[name]))
    for name, want, got in wrong:
        print(f"  FAIL {name}: file {want:g}, SITL reads {got:g}")
    ignored = sorted(set(re.findall(r"Ignored unknown param (\S+)", log)))
    if ignored:
        print("sitl: SITL's own report of names it does not know: " + ", ".join(ignored))
    print(f"sitl: {len(params) - len(missing) - len(wrong)} read back equal, {len(wrong)} differ")
    print(f"sitl: {len(missing)} not in SITL (hardware-only, cannot be checked here):")
    if missing:
        print("      " + ", ".join(missing))
    if not table_complete and missing:
        print("sitl: WARNING: the table was incomplete; a missing name was asked for 3 times")
    return 1 if wrong else 0


# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("check", choices=("lint", "meta", "sitl"))
    ap.add_argument("--file", type=Path, default=DEFAULT_PARAM_FILE, help="the parameter file")
    ap.add_argument("--release", default=DEFAULT_RELEASE, help="Copter release (meta)")
    ap.add_argument("--pdef", type=Path, default=None, help="apm.pdef.xml (meta)")
    ap.add_argument("--download", action="store_true", help="fetch apm.pdef.xml if missing (meta)")
    ap.add_argument("--dump", type=Path, default=None, help="write every SITL parameter (sitl)")
    args = ap.parse_args(argv)

    try:
        params = parse_param_file(args.file)
    except (OSError, ParamFileError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"{args.file.name}: {len(params)} parameters")

    if args.check == "lint":
        return run_lint(params)
    if args.check == "meta":
        pdef = args.pdef or default_pdef_path(args.release)
        if not pdef.is_file():
            if not args.download:
                print(f"error: {pdef} is missing; run with --download (network)", file=sys.stderr)
                return 2
            download_pdef(args.release, pdef)
        return run_meta(params, pdef, args.release)
    return run_sitl(params, args.file, args.dump)


if __name__ == "__main__":
    raise SystemExit(main())
