# D8.1 board-session runbook

## Progress (updated 2026-08-16 by the planning session)

| Step | State |
| --- | --- |
| Phase A (host work, image, harness) | Done, verified, committed |
| Phase J (J1 hashes, J2 reservations) | Done |
| Phase B (cards written, boards boot the recorded image, cards stay in) | Done |
| Phase C0 (discovery, identity on our image) | Done |
| Phase C1 (daemon provision, IVE budget gate, smoke) | **In progress** |
| C2-C8 | Pending C1 |
| Rig facts (MACs, credentials, router/switch) | `D8_1_RIG_DEBUG_LOG.md` |

**Status:** finalized 2026-08-10, approved by Samuel. Executes the D8.1
scope of `PHASE_D8_BRIEF.md` under the decisions recorded in the D0 log
("D8.1 opening (2026-08-10)"). An agent follows this file top to bottom;
steps marked HANDS are Samuel's and block the steps after them.

**Read first:** `/CLAUDE.md`, `PHASE_D8_BRIEF.md` (including both
amendments), the "D8.1-prep record" and "D8.1 opening" entries in
`DETECTION_CONTRACTS_D0.md`, report sections 2.1, 6.2, 8.1.

## Phase A — remaining host work (agent, no board)

A1. **Green gate platform.** On the provisioned Linux server (Azure) or
    the Linux mirror: one-time `sysctl -w net.core.rmem_max=8388608
    net.core.rmem_default=8388608` (W3 needs >= 4 MB receive buffer;
    record the values). Then the full suite + ruff. Expect all pass, no
    environment failures. This machine is the authoritative gate for
    every hand-back from now on; macOS runs are advisory (W6 decision).
A2. **Finish the image.** `scripts/build-image.sh` on the Linux mirror
    (native x86 Linux; the emulated-Mac path is what crashed).
    Drop `image-manifest.json` into `firmware/rv1106/image/`, re-run the
    report generator; sections 1 and 2.1 fill from it. Verify the
    manifest lists per-file SHA-256 for the full image set.
A3. **F10 fix** (sanctioned): catch protobuf `DecodeError` on health
    datagrams in `SocketIngestAdapter.poll`, count as rejected, add the
    test. Closes D8-F10.
A4. **RAM-loop source** (F8 decision): new injection source mode in the
    daemon — preload a declared clip (bounded frame count, full-res Y)
    into DDR at start, loop it at the requested rate or unpaced.
    Host-buildable and host-tested like every other source. Every sweep
    record carries: source mode, source byte rate, and the declared
    DDR-profile systematic note. RAM ceiling (D0 "D8.1 Phase A closure"):
    daemon total (clip + detector + fixed) <= 160 MB, enforced by the
    daemon's own startup check; tests gate against the daemon's printed
    total. Per-resolution clip defaults 12 / 78 / 171 frames, Provisional
    until C1 confirms.
A5. Suite green on the gate platform, adversarial review, hand back
    before any board step.

## Agent location (decided 2026-08-11)

The agent (Claude Code) runs on Samuel's Mac for the whole D8.1
hand-off; the rig (3 nodes + Jetson on the PoE switch, uplinked to the
router) is reached over the LAN. Moving the agent onto the Jetson is a
later option, not this phase. The Jetson suite run (old J2) is deferred
to D9 prep; the Azure gate remains the authoritative hand-back gate.

## Phase J — prep from the Mac (agent)

J1. Verify `image-manifest.json` hashes of `firmware/rv1106/image/` on
    the Mac — these are the bytes the SD cards carry. Record.
J2. Router hygiene: DHCP reservations for the three node MACs and the
    Jetson so addresses hold for the whole phase. Record the
    MAC -> IP -> physical-position table; Samuel labels node 1.

## Phase B — flash via SD (HANDS)

> Phase B and C0 were rewritten 2026-08-15 by the planning session per
> D8-F15 (report section 9): the card IS the system; there is no
> self-flash and no internal-storage write on this board config.
> Known quirk kept from the finding: `boot.img`'s built-in cmdline says
> `root=/dev/mmcblk1p7`, the env's `p6` wins via U-Boot substitution;
> a board hanging on the root filesystem points there first.

B1. Write `sd_update.img` to one microSD PER BOARD — the card is the
    node's system disk, not an installer (D8-F15). Record the image
    build; the manifest hash is the identity. macOS will call the card
    unreadable and offer to initialise it (no MBR/GPT by design) —
    decline.
B2. Card in, power on, board boots our image from the card. THE CARD
    STAYS IN — removing it removes the system. Nothing is written to
    internal storage; the stock image stays on SPI NAND, so rollback
    for any board is pulling its card. All boards may get cards in
    this pass; scored work stays node-1 only.
B3. Confirm each board boots the recorded image and appears on the
    network (MAC table in `D8_1_RIG_DEBUG_LOG.md`; reservations per
    J2). Serial console as fallback; a board hanging on the root
    filesystem: see the D8-F15 note on the p6/p7 cmdline mismatch.

## Phase C0 — discovery and identity (agent, from the Mac).
REWRITTEN 2026-08-15 per D8-F15

C0a. Sweep the subnet, map node MACs to IPs, match against J2's table
     and the rig debug log. SSH into each with the image's declared
     credentials.
C0b. Confirm image identity on every board: the image's version/build
     marker against the manifest record; `uname`; confirm the root
     device is the card (`/dev/mmcblk1p6`) and NAND was not touched.
C0c. The former network-reflash pass is DELETED: a node running from
     its card cannot rewrite the partition it runs from. The standing
     update paths this phase are: daemon binary via `provision.py`
     (hash-verified, userdata) — which C1 exercises — and full-image
     update by re-writing a card. A/B card layout / OTA is a
     D8.2-or-later design question, recorded, not a bench call.
C0d. Nodes 2 and 3: done after boot check — leave them
     powered and idle on the switch (their presence during node-1 work
     is recorded). Node 1 proceeds to C1.

## Phase C — board session (agent drives via provision.py; HANDS only
for power-cycles and cables)

C1. Provision the board daemon to node 1 (hash-verified push), start by
    hand (boot-into-daemon is D8.2). Smoke: 1 Hz health received,
    control-plane stop works, clean SIGTERM. **IVE budget confirmation
    (D8-F11):** the IVE arm compiles for the first time on real SDK
    hardware paths; read the daemon's RAM-budget INFO line at first
    start, record actual vs the arithmetic (119,439,360 B detector
    figure) in the report, and re-derive the clip defaults if they
    differ. If the actual total exceeds the 160 MB ceiling at any swept
    resolution, STOP before C3 and report — do not trim the clip
    silently.
C2. Ethernet C1 injection smoke at a rate the link carries (~10 MB/s):
    end-to-end packets decode on the host stack. Records the honest
    link byte rate (D8-F8 evidence).
C3. **Sweep** (RAM-loop source, unpaced): the three D4 resolutions on
    node 1. Collect sustained fps, peak RSS, A7 utilisation, thermals;
    DDR bandwidth if the board exposes counters, else NOT-MEASURED with
    reason. Two runs per resolution; E8 comparison within the declared
    section 8.1 bounds.
C4. **Resolution decision.** Highest resolution that sustains 30 fps
    with declared margin wins; intersect with the D4 scorecards (all
    three pass host quality gates, so the board ceiling decides). Draft
    the decisions-log entry (resolution Chosen, numbers Measured) for
    the planning session to review and append — the agent does NOT
    write the contracts doc.
C5. **Soak:** one hour, paced 30 fps, chosen resolution, health
    monitored throughout; any daemon restart, health gap, or thermal
    runaway fails the soak and is a finding.
C6. **CSI smoke** (HANDS to aim the camera): SC3336 frames via the
    debug stream, eyeballed only, not scored, no PTS claims.
C7. Report regeneration: build provenance (image manifest, SDK commit,
    board serial), sweep tables, E8 verdicts, soak record, F8 systematic
    note. Labels: board numbers Measured, the rest unchanged.
C8. Adversarial review, suite green on the gate platform, hand back.
    D8.2 does not start in the same hand-back.

## Standing constraints

Fenced paths untouched (v1/, golden/, contracts except entries this
runbook's decisions already sanction, proto/). No tuning on gate scenes.
Fixtures immutable this phase. Every scored artifact seeded; fabricated
PTS declared. If any step needs a decision this runbook does not cover,
stop and report — do not improvise a contract change.
