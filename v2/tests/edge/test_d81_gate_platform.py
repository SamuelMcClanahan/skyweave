"""D8.1 A1: the gate platform is recorded, and the report quotes what it was given.

The D0 "D8.1 opening" entry moved the authoritative all-green suite gate to a
provisioned Linux server and made macOS advisory, on the condition that the VM
raises `net.core.rmem_max`/`rmem_default` to at least the 4 MB W3 needs.
Runbook A1 says to RECORD the values.

Before D8.1 there was nowhere to record them. The evidence file had no host
field at all, so the report's only claim about provenance was "the machine that
generated this report" — which is true on every machine and therefore settles
nothing. These tests gate the three things that can now go wrong:

- a probe that reports a number it did not read (a zero for an absent knob is
  the exact failure `metrics.NOT_MEASURED` exists to prevent);
- a report that renders an absent block as if it were a present one;
- a report that renders `NOT-MEASURED` without the subject the discipline
  requires.

`generate` stays a pure function of repository + evidence: it reads this block
out of the dict and never probes a host. A test that let it probe would make
the freshness gate platform-dependent and it would fail on one of the two
machines this project runs on.
"""

from __future__ import annotations

from skyweave2.edge import metrics
from skyweave2.edge import report as report_module

# The two receive-buffer knobs carry DIFFERENT values, and the two NOT-MEASURED
# reasons are worded differently, because the finding "the gate-platform publish
# test cannot see a dropped or duplicated receive-buffer row" was reachable only
# because they were byte-identical: A1 asks for two per-knob values, and a single
# `"8388608" in block` is satisfied by one surviving row, by the same row printed
# twice, or by the two swapped. A1 runs `sysctl -w` on both knobs, so both are
# free to be at or above the line; the fixture keeps them apart so the assertion
# can tell them apart.
LINUX_GATE = {
    "system": "Linux",
    "release": "6.17.0-1022-azure",
    "machine": "x86_64",
    "distribution": "Ubuntu 24.04.4 LTS",
    "net.core.rmem_max": 8388608,
    "net.core.rmem_default": 6291456,
}

NO_PROCFS = {
    "system": "Darwin",
    "release": "25.5.0",
    "machine": "arm64",
    "distribution": "",
    "net.core.rmem_max": {metrics.NOT_MEASURED: "no /proc/sys on this platform"},
    "net.core.rmem_default": {
        metrics.NOT_MEASURED: "no /proc/sys on this platform either"
    },
}


SMALL_BUFFER_LINUX = dict(
    LINUX_GATE,
    **{"net.core.rmem_max": 212992, "net.core.rmem_default": 212992},
)

GREEN = {
    "selector": "tests", "returncode": 0, "summary": "470 passed, 1 skipped in 251.02s",
    "passed": 470, "failed": 0, "skipped": 1, "error": 0,
}
RED = {
    "selector": "tests", "returncode": 1,
    "summary": "1 failed, 468 passed, 1 skipped in 254.04s",
    "passed": 468, "failed": 1, "skipped": 1, "error": 0,
}

#: The sentence that retires W6. It may appear only when the recorded platform
#: IS the gate and the recorded row IS green.
CLAIM = "advisory-host record rather than an explanation of a red row"


def _flat(text: str) -> str:
    """The document with its hard wrap removed.

    The claims below are SENTENCES, and the generator wraps at whatever column
    reads well; asserting on a wrapped fragment would make a re-wrap look like a
    regression and, worse, would let a real regression hide behind one.
    """
    return " ".join(text.split())


def _section(text: str) -> str:
    """The gate-platform block, cut at the next heading of any level."""
    start = text.index("#### Which machine, and with which receive buffer")
    rest = text[start + 1 :]
    end = rest.find("\n#")
    return rest if end < 0 else rest[:end]


def test_the_probe_reports_a_reading_or_says_it_had_none():
    """`_gate_platform` runs on whichever machine this suite is on.

    Both platforms are asserted by the same test because the point is the
    DISCIPLINE, not the platform: either an int that came out of procfs, or a
    NOT-MEASURED carrying its reason. Never a zero, never a bare marker.
    """
    block = report_module._gate_platform()
    assert block["system"] in {"Linux", "Darwin"}
    assert block["release"]
    assert block["machine"]
    for knob in ("net.core.rmem_max", "net.core.rmem_default"):
        value = block[knob]
        if isinstance(value, dict):
            # An absence is a statement about a machine, and a statement needs
            # a subject.
            assert list(value) == [metrics.NOT_MEASURED]
            assert value[metrics.NOT_MEASURED].strip()
        else:
            assert isinstance(value, int) and value > 0


def test_the_report_publishes_the_gate_platform_it_was_given():
    """Every recorded field reaches the reader, including the buffer sizes.

    Each buffer is asserted as its own RENDERED ROW, label and value together.
    A bare ``"8388608" in block`` was the whole gate before, and against a
    fixture whose two knobs held the same number it survived a renderer that
    dropped the ``rmem_default`` row, printed ``rmem_max`` under both labels, or
    swapped the two — three mutants, none caught, for a table whose entire job
    is to say which knob is where.
    """
    text = report_module.generate({"gate_platform": LINUX_GATE})
    block = _section(text)
    assert "Ubuntu 24.04.4 LTS" in block
    assert "6.17.0-1022-azure" in block
    assert "x86_64" in block
    assert "| `net.core.rmem_max` | `8388608` |" in block
    assert "| `net.core.rmem_default` | `6291456` |" in block
    assert metrics.NOT_MEASURED not in block


def test_an_unrecorded_gate_platform_reads_pending_not_blank():
    """Absence must be visible.

    A table that simply had no rows would render as though the question had
    not been asked, which is the state this section exists to remove.
    """
    block = _section(report_module.generate({}))
    assert "PENDING (run `report measure`)" in block


def test_a_machine_without_procfs_says_so_rather_than_reporting_zero():
    """The macOS row. `NOT-MEASURED` carries its reason or it is not a claim.

    Per knob, for the same reason the Linux test is: two absences that render
    the same string are one substring check away from a table that publishes
    only one of them. The two reasons here are worded differently so the rows
    are distinguishable at all.
    """
    block = _section(report_module.generate({"gate_platform": NO_PROCFS}))
    assert (
        f"| `net.core.rmem_max` | {metrics.NOT_MEASURED} "
        "(no /proc/sys on this platform) |"
    ) in block
    assert (
        f"| `net.core.rmem_default` | {metrics.NOT_MEASURED} "
        "(no /proc/sys on this platform either) |"
    ) in block
    assert "Darwin" in block
    # A zero here would read as "the receive buffer is zero bytes", which is a
    # different and false statement from "this machine has no such knob".
    assert "| `0` |" not in block


# ---------------------------------------------------------------------------
# The claim that retires W6, which must be DERIVED and not asserted
# ---------------------------------------------------------------------------
#
# The finding: section 10 stated "the whole-suite row above is that machine's,
# and the paragraphs above are now an advisory-host record rather than an
# explanation of a red row" as an unconditional literal — four paragraphs under
# a table that could read PENDING or Darwin, and a suite row that could read one
# failure. `_section` cuts at the next heading, so the four tests above could
# not see it. These assert against the WHOLE document, in both directions, so
# neither can pass vacuously.


def test_the_gate_claim_is_made_on_the_gate_platform_with_a_green_row():
    text = _flat(report_module.generate(
        {"gate_platform": LINUX_GATE, "suites": {"full": GREEN}}
    ))
    assert CLAIM in text
    assert "**The whole-suite row above is that machine's.**" in text
    # The claim quotes the row it rests on, so a reader can check it.
    assert GREEN["summary"] in text
    # Same row, same discipline, five paragraphs earlier.
    assert "The run recorded above is a clean one" in text


def test_the_gate_claim_is_not_made_off_the_gate_platform():
    """A Darwin run is advisory whatever its suite row says."""
    text = _flat(report_module.generate(
        {"gate_platform": NO_PROCFS, "suites": {"full": GREEN}}
    ))
    assert CLAIM not in text
    assert "**The whole-suite row above is NOT that machine's.**" in text
    assert "Darwin" in text


def test_a_linux_box_under_the_declared_receive_buffer_is_not_the_gate():
    """A1's condition is Linux AND at least 4 MB, and both are load-bearing.

    A `net.core.rmem_max` of 212992 is the case the section's own caveat
    describes: W3 loses datagrams silently and fails on a COUNT mismatch.
    """
    text = _flat(report_module.generate(
        {"gate_platform": SMALL_BUFFER_LINUX, "suites": {"full": GREEN}}
    ))
    assert CLAIM not in text
    assert "**The whole-suite row above is NOT that machine's.**" in text
    assert str(report_module.GATE_RMEM_MAX_BYTES) in text


def test_a_red_row_on_the_gate_platform_is_reported_as_a_red_row():
    """The gate can be the gate and still fail. That retires nothing."""
    text = _flat(report_module.generate(
        {"gate_platform": LINUX_GATE, "suites": {"full": RED}}
    ))
    assert CLAIM not in text
    assert "and it is not green" in text
    assert RED["summary"] in text
    assert "The run recorded above is NOT a clean one" in text


def test_an_unrecorded_platform_says_so_instead_of_claiming_the_gate():
    text = _flat(report_module.generate({"suites": {"full": GREEN}}))
    assert CLAIM not in text
    assert "is NOT RECORDED" in text
