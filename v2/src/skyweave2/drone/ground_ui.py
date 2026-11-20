"""Ground UI: the operator's page, served by the companion process (DRONE_CONTRACTS_D0.md §7).

[U1] Plain HTTP from the standard library (``ThreadingHTTPServer``); a laptop
browser is the client. Three endpoints and no others:

- ``GET /``: the page.
- ``GET /state``: the JSON view plus the server-rendered status block. Each
  poll is one ground heartbeat into the core ([U6], [R2] ``ground_hb``).
- ``POST /command``: one command packet [P5]; the response is its ack.

[U2] Exactly four commands, from four buttons: ``prime`` (with a parameter
form whose defaults are :class:`packets.PrimeParams`), ``approve_engage``,
``mark_complete``, ``abort``. No joystick, no manual velocity, no other
command, ever: the page has no other control, the server no other endpoint,
and any other command name fails decoding and is refused ([C7], [P5b]).

[U3] The page draws a fresh random ``cmd_id`` per command
(``crypto.randomUUID()``, or the same v4 UUID from ``crypto.getRandomValues``
where the browser hides ``randomUUID`` outside a secure context, as it does on
plain HTTP over the field AP) and matches the ack by id. A lost answer is
re-sent with the same id and body, which [P5a] makes safe. The operator types
the token; the page keeps it only in its own memory (no storage, no cookie, no
URL, no form that could submit it).

[U4] The status block shows the state banner, the engaged track and the
candidate awaiting approval, battery, link health (FC link, attitude age, RC,
gate state, ground link), the trial echo, and recent events.

[U5] One render function. The view model is built from recording records by
:class:`ViewBuilder`: live, the companion's :class:`recording.Recorder` writes
through a :class:`RecordTap` that feeds every line it records to the builder;
offline, :func:`view_from_recording` feeds a recording file. So the live page
is rendered from the recorded packet stream itself, and a replayed recording
renders through exactly the same code (:func:`render_status`).

Commands ([P5b], [P5c]): :class:`CommandReceiver` is the one path from a
command's bytes to the core, for this page and for the UDP command port. It
decodes with the frozen decoder, compares the token in constant time over the
ASCII bytes (``hmac.compare_digest``), and hands the core only the result. A
body that fails decoding never reaches the mission: it is acked
``rejected_malformed`` when a ``cmd_id`` can be salvaged (the core records that
ack) and is counted and logged in every case. The token is never logged,
recorded, rendered, or put in ``meta.config``.

Threading: the companion core is single threaded (one input order, [R3]).
HTTP requests run on server threads, so every call into the core, fc_link, or
the view builder happens under one lock that the companion's loop also holds
for each of its iterations; the stamp of an HTTP input is read from the clock
inside that lock, so input stamps never decrease ([M2]).
"""

from __future__ import annotations

import hmac
import html
import logging
import threading
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, fields
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import IO, Any
from urllib.parse import urlsplit

from skyweave2.drone.core import CompanionCore, CoreConfig, CoreOutput
from skyweave2.drone.packets import (
    MAX_DATAGRAM_BYTES,
    AckPacket,
    CommandName,
    CommandPacket,
    FcLinkHealthPacket,
    MissionState,
    MissionStatePacket,
    PacketError,
    PacketKind,
    PrimeParams,
    TrackPacket,
    TrialEcho,
    TrialType,
    canonical_json,
    decode,
    encode,
)
from skyweave2.drone.recording import Record, RecordingError, Stream, parse_record, read_records
from skyweave2.drone.types import Clock
from skyweave2.drone.udp import LOOPBACK
from skyweave2.drone.vehicle_state import VehicleState, parse_frames

PAGE_PATH = "/"
STATE_PATH = "/state"
COMMAND_PATH = "/command"

POLL_MS = 500
"""Page state-poll period (Provisional, E1): ten heartbeats inside the 5 s
``ground_link_timeout_ms`` (T18)."""

RECENT_EVENTS = 16  # events kept for the page ([U4]); display only
TRACK_MEMORY = 32  # newest track packets kept, by id, for the engaged / candidate lines
RC_PREFIX = "rc:"  # [P5]: reserved for radio approvals [F7]

_JSON = "application/json"
_HTML = "text/html; charset=utf-8"

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# View model ([U4], [U5])
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class TrackView:
    """The newest track packet [P2] of one id, as the page shows it."""

    track_id: int
    state: str
    u: float
    v_px: float
    w: float
    h: float
    hits: int
    misses: int
    t_rx: int


@dataclass(frozen=True, kw_only=True)
class ViewModel:
    """What the page shows at one instant ``t`` (board ms).

    Mission fields come from the newest ``mission_state`` packet [P3]; the
    candidate from its ``candidate:`` event, while the state is ``ACQUIRING``
    (the only state with a candidate, [M8], [G9]). Link health is the newest
    ``fc_link_health`` packet [P4] verbatim (``health_t`` is its stamp); before
    the first one, ``gate_state`` is the recording's configured gate. Battery,
    mode and armed come from the FC's own frames through
    :class:`vehicle_state.VehicleState` at ``t`` ([M2a]: unknown while the link
    is down). The ground link is the newest ground heartbeat: a UI poll or an
    authenticated ground command ([R2]).
    """

    t: int
    mission_state: str | None  # None: unprimed ([P3a])
    engaged_track_id: int | None
    engaged_track: TrackView | None
    candidate_id: int | None
    candidate_track: TrackView | None
    trial: TrialEcho | None
    battery_pct: float | None
    fc_mode: str | None
    armed: bool | None
    health_t: int | None
    fc_link_up: bool | None
    attitude_age_ms: int | None
    rc_seen: bool | None
    gate_state: str
    last_setpoint_t: int | None
    ground_hb_t: int | None
    ground_link_up: bool | None
    events: tuple[tuple[int, str], ...]  # oldest first, at most RECENT_EVENTS

    def to_obj(self) -> dict[str, Any]:
        """The JSON object of ``GET /state`` (``view``)."""
        return {
            "t": self.t,
            "mission_state": self.mission_state,
            "engaged_track_id": self.engaged_track_id,
            "engaged_track": _track_obj(self.engaged_track),
            "candidate_id": self.candidate_id,
            "candidate_track": _track_obj(self.candidate_track),
            "trial": None if self.trial is None else _trial_obj(self.trial),
            "battery_pct": self.battery_pct,
            "fc_mode": self.fc_mode,
            "armed": self.armed,
            "fc_link": {
                "t": self.health_t,
                "fc_link_up": self.fc_link_up,
                "attitude_age_ms": self.attitude_age_ms,
                "rc_seen": self.rc_seen,
                "gate_state": self.gate_state,
                "last_setpoint_t": self.last_setpoint_t,
            },
            "ground_link": {"last_hb_t": self.ground_hb_t, "up": self.ground_link_up},
            "events": [{"t": t, "name": name} for t, name in self.events],
        }


def _track_obj(track: TrackView | None) -> dict[str, Any] | None:
    return None if track is None else asdict(track)


def _trial_obj(trial: TrialEcho) -> dict[str, Any]:
    out = {f.name: getattr(trial, f.name) for f in fields(trial)}
    out["trial_type"] = trial.trial_type.value
    return out


def _event_id(name: str, prefix: str) -> int | None:
    head, _, tail = name.partition(":")
    if head != prefix:
        return None
    try:
        return int(tail)
    except ValueError:
        return None  # receivers ignore what they cannot read (§4.6)


class ViewBuilder:
    """Folds recording records [R2] into the page's view model ([U5]).

    The first record must be ``meta``: it carries the configuration (link
    constants for the vehicle parser, the ground link timeout, the gate).
    """

    def __init__(self, recent_events: int = RECENT_EVENTS) -> None:
        self._config: CoreConfig | None = None
        self._vehicle: VehicleState | None = None
        self._t: int | None = None
        self._state: MissionState | None = None
        self._engaged: int | None = None
        self._candidate: int | None = None
        self._trial: TrialEcho | None = None
        self._health: FcLinkHealthPacket | None = None
        self._ground_hb: int | None = None
        self._tracks: OrderedDict[int, TrackView] = OrderedDict()
        self._events: deque[tuple[int, str]] = deque(maxlen=recent_events)

    def feed(self, record: Record) -> None:
        """One record, in file order."""
        if record.stream is Stream.META:
            if self._config is not None:
                raise RecordingError("a recording has exactly one meta record ([R2])")
            try:
                self._config = CoreConfig.from_obj(record.config)
            except ValueError as exc:
                raise RecordingError(f"meta.config: {exc}") from exc
            self._vehicle = VehicleState(self._config.link)
            self._t = record.t_rx
            return
        if self._vehicle is None:
            raise RecordingError("a recording starts with its meta record ([R2])")
        t = record.t_rx
        self._t = t if self._t is None else max(self._t, t)
        pkt = record.packet
        if isinstance(pkt, MissionStatePacket):
            self._mission_state(pkt)
        elif isinstance(pkt, FcLinkHealthPacket):
            self._health = pkt
        elif isinstance(pkt, TrackPacket):
            self._track(pkt, t)
        elif record.stream is Stream.MAVLINK and record.direction == "rx":
            assert record.raw is not None
            for msg in parse_frames(record.raw):
                self._vehicle.ingest(msg, t)
        elif record.stream is Stream.GROUND_HB:
            self._ground_hb = t
        elif record.stream is Stream.COMMAND and record.auth_ok:
            self._ground_hb = t  # [R2]: an authenticated ground command is a heartbeat

    def _mission_state(self, pkt: MissionStatePacket) -> None:
        self._state = pkt.mission_state
        self._engaged = pkt.engaged_track_id
        self._trial = pkt.trial
        for ev in pkt.events:
            self._events.append((ev.t, ev.name))
            cand = _event_id(ev.name, "candidate")
            if cand is not None:
                self._candidate = cand
        if pkt.mission_state is not MissionState.ACQUIRING:
            self._candidate = None  # a candidate exists only in ACQUIRING ([M8], [G9])

    def _track(self, pkt: TrackPacket, t: int) -> None:
        self._tracks[pkt.track_id] = TrackView(
            track_id=pkt.track_id,
            state=pkt.state.value,
            u=pkt.u,
            v_px=pkt.v_px,
            w=pkt.w,
            h=pkt.h,
            hits=pkt.hits,
            misses=pkt.misses,
            t_rx=t,
        )
        self._tracks.move_to_end(pkt.track_id)
        while len(self._tracks) > TRACK_MEMORY:
            self._tracks.popitem(last=False)

    def build(self, now: int | None = None) -> ViewModel:
        """The view at board ms ``now`` (default: the newest record's stamp)."""
        if self._config is None or self._vehicle is None or self._t is None:
            raise RecordingError("no meta record yet ([R2])")
        t = self._t if now is None else now
        snap = self._vehicle.snapshot(t)
        health = self._health
        hb = self._ground_hb
        return ViewModel(
            t=t,
            mission_state=None if self._state is None else self._state.value,
            engaged_track_id=self._engaged,
            engaged_track=None if self._engaged is None else self._tracks.get(self._engaged),
            candidate_id=self._candidate,
            candidate_track=None if self._candidate is None else self._tracks.get(self._candidate),
            trial=self._trial,
            battery_pct=snap.battery_pct,
            fc_mode=snap.mode,
            armed=snap.armed,
            health_t=None if health is None else health.t,
            fc_link_up=None if health is None else health.fc_link_up,
            attitude_age_ms=None if health is None else health.attitude_age_ms,
            rc_seen=None if health is None else health.rc_seen,
            gate_state=(self._config.gate if health is None else health.gate_state).value,
            last_setpoint_t=None if health is None else health.last_setpoint_t,
            ground_hb_t=hb,
            ground_link_up=(
                None if hb is None else t - hb <= self._config.mission.ground_link_timeout_ms
            ),
            events=tuple(self._events),
        )


class RecordTap:
    """A :class:`recording.Recorder` sink that also feeds every line it writes
    to a :class:`ViewBuilder`, so the live page renders from the recorded
    stream itself ([U5]). ``downstream`` is the recording file."""

    def __init__(self, builder: ViewBuilder, downstream: IO[str] | None = None) -> None:
        self._builder = builder
        self._down = downstream
        self._buf = ""
        self.view_errors = 0  # lines the view could not take; the recording keeps them

    def write(self, text: str) -> int:
        if self._down is not None:
            self._down.write(text)
        self._buf += text
        while "\n" in self._buf:
            line, _, self._buf = self._buf.partition("\n")
            if line.strip():
                try:
                    self._builder.feed(parse_record(line))
                except Exception:  # noqa: BLE001 - the view is display only, never fatal
                    self.view_errors += 1
                    log.exception("UI view could not take a recorded line; recording kept it")
        return len(text)

    def flush(self) -> None:
        if self._down is not None:
            self._down.flush()


def view_from_recording(
    source: str | Path | Iterable[str],
    *,
    until_t: int | None = None,
    recent_events: int = RECENT_EVENTS,
) -> ViewModel:
    """[U5]: the page's view model from a recording, as of ``until_t`` (the
    records with ``t_rx <= until_t``; default: the whole recording)."""
    builder = ViewBuilder(recent_events)
    for record in read_records(source):
        if until_t is not None and record.t_rx > until_t:
            break
        builder.feed(record)
    return builder.build(until_t)


# ---------------------------------------------------------------------------
# Render ([U4], [U5]): pure functions of the view model
# ---------------------------------------------------------------------------


def _esc(x: object) -> str:
    return html.escape(str(x), quote=True)


def _dv(value: Any) -> str:
    """``data-value``: the raw value as canonical JSON, for machines and tests."""
    return _esc(canonical_json(value).decode("ascii"))


def _row(label: str, elem_id: str, value: Any, text: str) -> str:
    return (
        f'<tr><th>{_esc(label)}</th><td id="{elem_id}" data-value="{_dv(value)}">'
        f"{_esc(text)}</td></tr>"
    )


def _track_text(track_id: int | None, track: TrackView | None) -> str:
    if track_id is None:
        return "none"
    if track is None:
        return f"#{track_id}"
    return (
        f"#{track_id} · {track.state} · {track.w:.0f}×{track.h:.0f} px at "
        f"({track.u:.0f}, {track.v_px:.0f}) · hits {track.hits} · misses {track.misses}"
    )


def _onoff(value: bool | None, on: str, off: str, unknown: str) -> str:
    if value is None:
        return unknown
    return on if value else off


def render_status(view: ViewModel) -> str:
    """The status block of the page ([U4]): the one render function that serves
    the live page and a replayed recording alike ([U5])."""
    state = view.mission_state or "UNPRIMED"
    t = view.t
    no_health = "no health packet yet"
    if view.ground_hb_t is None:
        ground = "no heartbeat yet"
    else:
        up = "up" if view.ground_link_up else "LOST"
        ground = f"{up} \u00b7 last heartbeat {t - view.ground_hb_t} ms ago"
    track_rows = [
        _row(
            "Engaged track",
            "engaged",
            view.engaged_track_id,
            _track_text(view.engaged_track_id, view.engaged_track),
        ),
        _row(
            "Candidate awaiting approval",
            "candidate",
            view.candidate_id,
            _track_text(view.candidate_id, view.candidate_track),
        ),
    ]
    vehicle_rows = [
        _row(
            "Battery",
            "battery",
            view.battery_pct,
            "unknown" if view.battery_pct is None else f"{view.battery_pct:.0f} %",
        ),
        _row("FC mode", "fc-mode", view.fc_mode, view.fc_mode or "unknown"),
        _row("Armed", "armed", view.armed, _onoff(view.armed, "ARMED", "disarmed", "unknown")),
    ]
    link_rows = [
        _row(
            "FC link", "fc-link", view.fc_link_up, _onoff(view.fc_link_up, "up", "DOWN", no_health)
        ),
        _row(
            "Attitude age",
            "attitude-age",
            view.attitude_age_ms,
            "none received" if view.attitude_age_ms is None else f"{view.attitude_age_ms} ms",
        ),
        _row("RC", "rc", view.rc_seen, _onoff(view.rc_seen, "seen", "NOT SEEN", no_health)),
        _row("Setpoint gate", "gate", view.gate_state, view.gate_state),
        _row(
            "Last setpoint",
            "last-setpoint",
            view.last_setpoint_t,
            "none" if view.last_setpoint_t is None else f"t = {view.last_setpoint_t} ms",
        ),
        _row("Ground link", "ground-link", view.ground_link_up, ground),
        _row(
            "Health packet",
            "health-t",
            view.health_t,
            "none yet" if view.health_t is None else f"t = {view.health_t} ms",
        ),
    ]
    if view.trial is None:
        trial_rows = ['<tr><td colspan="2">not primed</td></tr>']
    else:
        trial_rows = [
            _row(name, f"trial-{name}", value, str(value))
            for name, value in _trial_obj(view.trial).items()
        ]
    events = "".join(
        f'<li><span class="t">{_esc(et)}</span> {_esc(name)}</li>'
        for et, name in reversed(view.events)
    )
    return (
        f'<div id="banner" class="banner st-{_esc(state)}" data-value="{_dv(view.mission_state)}">'
        f"{_esc(state)}</div>"
        f'<p class="asof">companion t = {_esc(t)} ms</p>'
        '<div class="grid">'
        f"{_table('Track', track_rows)}{_table('Vehicle', vehicle_rows)}"
        f"{_table('Link health', link_rows)}{_table('Trial', trial_rows)}"
        "</div>"
        f'<h2>Recent events</h2><ol id="events" class="events">{events}</ol>'
    )


def _table(title: str, rows: list[str]) -> str:
    return f"<section><h2>{_esc(title)}</h2><table>{''.join(rows)}</table></section>"


_UNITS = {
    "v_max": "m/s",
    "alpha": "of 1920 px",
    "beta": "of 1920 px",
    "k": "hits",
    "pass_budget": "passes",
    "search_alt": "m",
    "d_s": "m",
    "target_width_m": "m",
    "flight_time_cap_s": "s",
    "battery_floor_pct": "%",
    "geofence_radius_m": "m",
}


def _param_inputs() -> str:
    """[U2] the prime parameter form; every default is ``PrimeParams()``."""
    defaults = PrimeParams()
    out: list[str] = []
    for f in fields(PrimeParams):
        name = f.name
        val = getattr(defaults, name)
        pid = f"p-{name}"
        if isinstance(val, TrialType):
            opts = "".join(
                f'<option value="{_esc(tt.value)}"{" selected" if tt is val else ""}>'
                f"{_esc(tt.value)}</option>"
                for tt in TrialType
            )
            ctl = f'<select id="{pid}" data-param="{name}" data-kind="enum">{opts}</select>'
        elif isinstance(val, bool):
            attrs = f'type="checkbox" data-kind="bool"{" checked" if val else ""}'
            ctl = f'<input id="{pid}" {attrs} data-param="{name}">'
        elif isinstance(val, int):
            ctl = (
                f'<input id="{pid}" type="number" step="1" data-param="{name}" '
                f'data-kind="int" value="{val}">'
            )
        else:
            ctl = (
                f'<input id="{pid}" type="number" step="any" data-param="{name}" '
                f'data-kind="float" value="{val!r}">'
            )
        unit = _UNITS.get(name, "")
        out.append(f'<label for="{pid}">{_esc(name)}</label>{ctl}<span>{_esc(unit)}</span>')
    return "".join(out)


_BUTTONS = (
    (CommandName.APPROVE_ENGAGE, "approve", "Approve engage"),
    (CommandName.MARK_COMPLETE, "complete", "Mark complete"),
    (CommandName.ABORT, "abort", "ABORT"),
)

_CSS = """
:root{color-scheme:dark;--bg:#111;--fg:#eee;--dim:#999;--ok:#2e7d32;--bad:#c62828;--warn:#b26a00}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.4 system-ui,sans-serif}
header{display:flex;gap:1em;align-items:baseline;padding:.5em 1em;border-bottom:1px solid #333}
h1{font-size:1.1em;margin:0}h2{font-size:1em;margin:.8em 0 .3em}h3{margin:.8em 0 .3em}
main{display:grid;grid-template-columns:minmax(0,3fr) minmax(0,2fr);gap:1em;padding:1em}
@media(max-width:900px){main{grid-template-columns:1fr}}
.banner{font-size:2.4em;font-weight:700;text-align:center;padding:.3em;border-radius:6px;background:#333}
.st-ACQUIRING,.st-LOST{background:var(--warn)}
.st-ENGAGED,.st-COASTING,.st-TOUCH{background:#8e24aa}
.st-COMPLETE,.st-MISS{background:var(--ok)}
.st-RETURN,.st-ABORT{background:var(--bad)}
.st-LAUNCH,.st-SEARCH{background:#1565c0}
.asof{color:var(--dim);margin:.3em 0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:.5em 1.5em}
table{border-collapse:collapse;width:100%}th{text-align:left;color:var(--dim);font-weight:400}
th,td{padding:.15em .4em;border-bottom:1px solid #222;vertical-align:top}
.events{font-family:ui-monospace,monospace;font-size:.9em;max-height:16em;overflow:auto}
.t{color:var(--dim)}
.params{display:grid;grid-template-columns:auto 1fr auto;gap:.3em .6em;align-items:center}
input,select{background:#222;color:var(--fg);border:1px solid #444;padding:.25em}
.cmds{display:flex;flex-wrap:wrap;gap:.5em;margin:.5em 0}
button.cmd{font-size:1.1em;padding:.6em 1em;border:0;border-radius:6px;color:#fff}
button.complete{background:#455a64}
button.abort{background:var(--bad);font-weight:700;flex:1 0 100%;font-size:1.5em}
button.approve{background:#6a1b9a}button.prime{background:#1565c0;margin-top:.5em}
.note{color:var(--dim);font-size:.85em;margin:.2em 0}
.ok{color:#81c784}.bad{color:#ef9a9a}.warn{color:#ffcc80}
#acks{font-family:ui-monospace,monospace;font-size:.85em}
"""

_SCRIPT = """
(function () {
  "use strict";
  var POLL_MS = @@POLL_MS@@;
  var RETRIES = 3;
  var statusEl = document.getElementById("status");
  var pollEl = document.getElementById("poll");
  var acksEl = document.getElementById("acks");
  var tokenEl = document.getElementById("ui-token");
  var misses = 0;

  function hex(bytes) {
    var s = "";
    for (var i = 0; i < bytes.length; i++) s += (bytes[i] + 0x100).toString(16).slice(1);
    return s;
  }

  // [U3] a fresh random cmd_id per command, never a counter. randomUUID exists
  // only in secure contexts; on plain HTTP the same v4 UUID comes from
  // getRandomValues.
  function freshId() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return "ui-" + window.crypto.randomUUID();
    }
    var b = new Uint8Array(16);
    window.crypto.getRandomValues(b);
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    var h = hex(b);
    return "ui-" + h.slice(0, 8) + "-" + h.slice(8, 12) + "-" + h.slice(12, 16) + "-" +
      h.slice(16, 20) + "-" + h.slice(20);
  }

  // [U6] every poll is a ground heartbeat. [U5] the server renders the status
  // block with the same function that renders a recording.
  function poll() {
    fetch("/state", {cache: "no-store"})
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (s) {
        statusEl.innerHTML = s.html;
        misses = 0;
        pollEl.textContent = "companion answering";
        pollEl.className = "ok";
      })
      .catch(function () {
        misses += 1;
        pollEl.textContent = "no answer from the companion (" + misses + " polls)";
        pollEl.className = "bad";
      })
      .then(function () { setTimeout(poll, POLL_MS); });
  }

  function primeParams() {
    var out = {};
    var els = document.querySelectorAll("[data-param]");
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      var name = el.getAttribute("data-param");
      var kind = el.getAttribute("data-kind");
      if (kind === "bool") out[name] = el.checked;
      else if (kind === "enum") out[name] = el.value;
      else out[name] = Number(el.value);  // a bad entry encodes as null: rejected_params
    }
    return out;
  }

  function ackRow(command, id) {
    var li = document.createElement("li");
    var res = document.createElement("span");
    li.appendChild(document.createTextNode(command + " " + id + " "));
    li.appendChild(res);
    acksEl.insertBefore(li, acksEl.firstChild);
    show(res, "sent", "warn");
    return res;
  }

  function show(res, text, cls) {
    res.textContent = text;
    res.className = cls;
  }

  // [U3], [P5a] ack by id. A lost answer is re-sent with the SAME cmd_id and
  // body: it executes at most once and returns the stored ack.
  function attempt(body, id, res, n) {
    fetch("/command", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: body,
      cache: "no-store"
    })
      .then(function (r) {
        return r.json().then(function (j) { return {status: r.status, ack: j}; });
      })
      .then(function (a) {
        if (a.status !== 200) {
          show(res, "refused: " + (a.ack.error || ("HTTP " + a.status)), "bad");
        } else if (a.ack.cmd_id !== id) {
          show(res, "ack for another id (" + a.ack.cmd_id + ")", "bad");
        } else {
          show(res, a.ack.result, a.ack.result === "accepted" ? "ok" : "bad");
        }
      })
      .catch(function () {
        if (n < RETRIES) {
          show(res, "no ack yet; re-sending the same id", "warn");
          setTimeout(function () { attempt(body, id, res, n + 1); }, 300);
        } else {
          show(res, "no ack", "bad");
        }
      });
  }

  function send(command) {
    var id = freshId();
    var res = ackRow(command, id);
    var tok = tokenEl.value;  // [U3] page memory only
    if (!tok) {
      show(res, "not sent: type the token first", "bad");
      return;
    }
    var pkt = {v: 1, cmd_id: id, token: tok, command: command};
    if (command === "prime") pkt.params = primeParams();
    attempt(JSON.stringify(pkt), id, res, 1);
  }

  var buttons = document.querySelectorAll("button[data-command]");
  for (var i = 0; i < buttons.length; i++) {
    buttons[i].addEventListener("click", function (ev) {
      send(ev.currentTarget.getAttribute("data-command"));
    });
  }
  poll();
})();
"""


def render_page(view: ViewModel, *, poll_ms: int = POLL_MS) -> str:
    """The whole page ([U1]-[U4]): the status block from :func:`render_status`,
    the four command buttons, the prime form, and the polling script."""
    buttons = "".join(
        f'<button type="button" class="cmd {cls}" data-command="{cmd.value}">{_esc(label)}</button>'
        for cmd, cls, label in _BUTTONS
    )
    prime = (
        f'<button type="button" class="cmd prime" data-command="{CommandName.PRIME.value}">'
        "Prime</button>"
    )
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>SkyWeave ground</title><style>{_CSS}</style></head><body>"
        '<header><h1>SkyWeave ground</h1><span id="poll" class="warn">not polled yet</span>'
        "</header><main>"
        f'<div id="status">{render_status(view)}</div>'
        '<section class="controls"><h2>Commands</h2>'
        '<label for="ui-token">Token</label> '
        '<input id="ui-token" type="password" autocomplete="off" spellcheck="false" size="24">'
        '<p class="note">Kept in this page only; type it again after a reload.</p>'
        f'<div class="cmds">{buttons}</div>'
        f'<h3>Prime</h3><div id="prime-params" class="params">{_param_inputs()}</div>{prime}'
        '<h3>Acks</h3><ol id="acks"></ol></section></main>'
        f"<script>{_SCRIPT.replace('@@POLL_MS@@', str(int(poll_ms)))}</script>"
        "</body></html>\n"
    )


# ---------------------------------------------------------------------------
# Commands ([P5a]-[P5c])
# ---------------------------------------------------------------------------


class CommandReceiver:
    """The one path from a command's bytes to the core ([P5b], [P5c]), for the
    page's ``POST /command`` and the UDP command port. The caller holds the
    core's lock and passes the input stamp ``t``.

    A command whose ``cmd_id`` uses the reserved ``rc:`` prefix ([P5], [F7])
    is a packet the contract does not allow from the ground, so it is refused
    like any other undecodable command ([C7], [P5b]): acked
    ``rejected_malformed``, never reaching the mission, so it can never occupy
    a radio approve's id in the [P5a] table.
    """

    def __init__(
        self,
        core: CompanionCore,
        ui_token: str,
        forward: Callable[[CoreOutput], None],
    ) -> None:
        # [P5c]: the configured token satisfies the field's own pattern; the
        # frozen decoder is the one place that pattern lives.
        try:
            encode(CommandPacket(cmd_id="token-check", token=ui_token, command=CommandName.ABORT))
        except PacketError:
            raise ValueError(
                "the UI token must be 1-256 chars of printable ASCII [!-~] ([P5c])"
            ) from None
        self._core = core
        self._expected = ui_token.encode("ascii")
        self._forward = forward
        self.received = 0
        self.malformed = 0
        self.auth_failed = 0

    def receive(self, data: bytes, t: int) -> AckPacket | None:
        """Decode, authenticate, and execute one command at stamp ``t``.

        Returns its ack, or ``None`` for an undecodable body with no readable
        ``cmd_id`` ([P5b]). The core's output goes to ``forward``.
        """
        self.received += 1
        try:
            cmd = decode(PacketKind.COMMAND, data)
        except PacketError as exc:
            return self._malformed(data, t, type(exc).__name__)
        assert isinstance(cmd, CommandPacket)
        if cmd.cmd_id.startswith(RC_PREFIX):
            return self._malformed(data, t, "reserved rc: cmd_id from the ground")
        auth_ok = hmac.compare_digest(cmd.token.encode("ascii"), self._expected)  # [P5c]
        if not auth_ok:
            self.auth_failed += 1
        out = self._core.on_command(cmd, auth_ok, t)
        self._forward(out)
        ack = out.acks[0]
        log.info("command %s %s at t=%d: %s", cmd.cmd_id, cmd.command.value, t, ack.result.value)
        return ack

    def _malformed(self, data: bytes, t: int, reason: str) -> AckPacket | None:
        self.malformed += 1
        ack = self._core.on_malformed_command(data, t)  # records the ack ([P5b])
        log.warning(
            "malformed command (%d B, %s) at t=%d: %s",
            len(data),
            reason,
            t,
            "no cmd_id; not acked" if ack is None else f"{ack.cmd_id} acked {ack.result.value}",
        )
        return ack


# ---------------------------------------------------------------------------
# Service and server ([U1], [U6])
# ---------------------------------------------------------------------------


class GroundUiService:
    """What the HTTP endpoints do, each call under :attr:`lock` ([U1]).

    ``builder`` must be fed by the core's recorder (through a
    :class:`RecordTap`), so the view follows every record. ``forward`` takes
    each core output to fc_link and the publishers (the companion's job; a
    UI poll can fire an AUTO row, so its output is forwarded too).
    """

    def __init__(
        self,
        core: CompanionCore,
        builder: ViewBuilder,
        clock: Clock,
        ui_token: str,
        *,
        forward: Callable[[CoreOutput], None] | None = None,
        lock: threading.Lock | None = None,
        poll_ms: int = POLL_MS,
    ) -> None:
        self.core = core
        self.builder = builder
        self.clock = clock
        self.lock = lock if lock is not None else threading.Lock()
        self.poll_ms = poll_ms
        self._forward: Callable[[CoreOutput], None] = forward or (lambda out: None)
        self.receiver = CommandReceiver(core, ui_token, self._forward)
        self.polls = 0

    def page(self) -> str:
        """``GET /``: the page at now. Fetching the page is not a state poll."""
        with self.lock:
            view = self.builder.build(self.clock())
        return render_page(view, poll_ms=self.poll_ms)

    def state(self) -> dict[str, Any]:
        """``GET /state``: one ground heartbeat ([U6]), then the view after it."""
        with self.lock:
            t = self.clock()
            self._forward(self.core.on_ground_heartbeat(t))
            view = self.builder.build(t)
            self.polls += 1
        return {"view": view.to_obj(), "html": render_status(view)}

    def command(self, body: bytes) -> AckPacket | None:
        """``POST /command``: one command packet; its ack ([U1], [U3])."""
        with self.lock:
            return self.receiver.receive(body, self.clock())


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], service: GroundUiService) -> None:
        self.service = service
        super().__init__(address, _Handler)


class _Handler(BaseHTTPRequestHandler):
    server: _Server
    server_version = "skyweave-ground/1"
    sys_version = ""
    timeout = 5.0  # a stalled client cannot hold a request thread

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        service = self.server.service
        if path == PAGE_PATH:
            self._reply(200, _HTML, service.page().encode("utf-8"))
        elif path == STATE_PATH:
            self._reply(200, _JSON, canonical_json(service.state()))
        else:
            self._reply(404, _JSON, canonical_json({"error": "not found"}))

    def do_POST(self) -> None:
        if urlsplit(self.path).path != COMMAND_PATH:
            self._reply(404, _JSON, canonical_json({"error": "not found"}))
            return
        ack = self.server.service.command(self._body())
        if ack is None:
            self._reply(400, _JSON, canonical_json({"error": "malformed command, no cmd_id"}))
        else:
            self._reply(200, _JSON, encode(ack))

    def _body(self) -> bytes:
        """At most one datagram's worth ([C9]); anything else reads as empty,
        which fails decoding with no cmd_id ([P5b])."""
        try:
            n = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return b""
        if not 0 <= n <= MAX_DATAGRAM_BYTES:
            return b""
        return self.rfile.read(n)

    def _reply(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        # The request line never carries the token (it travels in POST bodies only).
        log.debug("ui %s " + fmt, self.address_string(), *args)


class GroundUiServer:
    """The page's HTTP server on its own daemon thread ([U1])."""

    def __init__(self, service: GroundUiService, host: str = LOOPBACK, port: int = 0) -> None:
        self._httpd = _Server((host, port), service)
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._httpd.server_address[:2]
        return (str(host), int(port))

    @property
    def url(self) -> str:
        host, port = self.address
        return f"http://{host}:{port}/"

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            kwargs={"poll_interval": 0.05},
            name="ground-ui",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        if self._thread is not None:
            self._httpd.shutdown()
            self._thread.join()
            self._thread = None
        self._httpd.server_close()

    def __enter__(self) -> GroundUiServer:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
