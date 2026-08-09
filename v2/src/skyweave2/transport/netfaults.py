"""Datagram-level fault injection, applied at the real send path (D7 scope 6).

D6 injected Tier IV faults by manipulating an observation LIST. This module
injects the same axes one layer lower — on the datagrams themselves, at the
socket boundary — so the two mechanisms can be compared. Divergence between
them is a finding, which is the whole point of re-running Tier IV here.

Determinism: every decision is a pure function of (seed, camera_id,
frame_seq, axis), via the same ``zlib.crc32`` tag hashing D6 adopted after
Python's salted ``hash()`` made a campaign non-reproducible across runs. No
wall-clock and no global RNG state enter any decision.

Delay is expressed in DATAGRAM POSITIONS, not seconds: a released-later
datagram must land at a reproducible place in the arrival order, and a
sleep-based delay would make the delivered-event stream a race.
"""

from __future__ import annotations

import zlib
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np


def _rng(seed: int, *tags: int | str) -> np.random.Generator:
    entropy = [seed] + [
        tag if isinstance(tag, int) else zlib.crc32(tag.encode("utf-8"))
        for tag in tags
    ]
    return np.random.default_rng(np.random.SeedSequence(entropy))


@dataclass
class NetworkFaultPolicy:
    """What the emulated network does to each measurement datagram."""

    seed: int = 7
    loss_pct: float = 0.0
    duplication_pct: float = 0.0
    # Held back and released this many datagram positions later.
    lateness_pct: float = 0.0
    lateness_positions: int = 30
    # Swap order within a sliding window of this many datagrams (0 = off).
    reorder_window: int = 0
    # Cameras that stop transmitting entirely from this frame_seq onward.
    dropout: dict[int, int] = field(default_factory=dict)


@dataclass
class NetworkFaultLedger:
    """What the emulator actually did — the can-fail evidence for W5."""

    offered: int = 0
    sent: int = 0
    dropped: list[tuple[int, int]] = field(default_factory=list)
    duplicated: list[tuple[int, int]] = field(default_factory=list)
    delayed: list[tuple[int, int]] = field(default_factory=list)
    reordered: int = 0
    dropped_out: list[tuple[int, int]] = field(default_factory=list)


class FaultyDatagramSender:
    """Wraps a real send callable with the emulated network.

    ``send`` is the underlying transmit (a real ``UdpSender.send``). Datagrams
    the policy holds back are released by :meth:`flush`, which the replay loop
    calls once the recording is exhausted — a delayed datagram is late, not
    lost, and the aligner's lateness policy is what must catch it.
    """

    def __init__(
        self,
        send: Callable[[bytes], object],
        policy: NetworkFaultPolicy,
    ) -> None:
        self._send = send
        self._policy = policy
        self.ledger = NetworkFaultLedger()
        self._position = 0
        self._held: list[tuple[int, bytes, tuple[int, int]]] = []
        self._reorder_block: list[tuple[bytes, tuple[int, int]]] = []

    def _release_due(self) -> None:
        still_held = []
        for due, datagram, key in self._held:
            if due <= self._position:
                self._emit(datagram, key, count_position=False)
            else:
                still_held.append((due, datagram, key))
        self._held = still_held

    def _emit(self, datagram: bytes, key: tuple[int, int],
              count_position: bool = True) -> None:
        self._send(datagram)
        self.ledger.sent += 1
        if count_position:
            self._position += 1

    def _flush_reorder_block(self) -> None:
        if not self._reorder_block:
            return
        block = self._reorder_block
        self._reorder_block = []
        order = _rng(self._policy.seed, "reorder", self._position).permutation(
            len(block)
        )
        if list(order) != sorted(order):
            self.ledger.reordered += len(block)
        for index in order:
            datagram, key = block[index]
            self._emit(datagram, key)

    def offer(self, datagram: bytes, camera_id: int, frame_seq: int) -> None:
        """Offer one framed datagram to the emulated network."""
        policy = self._policy
        key = (camera_id, frame_seq)
        self.ledger.offered += 1
        self._release_due()

        cutoff = policy.dropout.get(camera_id)
        if cutoff is not None and frame_seq >= cutoff:
            self.ledger.dropped_out.append(key)
            return

        if policy.loss_pct > 0.0:
            roll = float(_rng(policy.seed, camera_id, frame_seq, "loss").uniform(0, 100))
            if roll < policy.loss_pct:
                self.ledger.dropped.append(key)
                return

        if policy.lateness_pct > 0.0:
            roll = float(_rng(policy.seed, camera_id, frame_seq, "late").uniform(0, 100))
            if roll < policy.lateness_pct:
                self._held.append(
                    (self._position + policy.lateness_positions, datagram, key)
                )
                self.ledger.delayed.append(key)
                return

        if policy.reorder_window > 1:
            self._reorder_block.append((datagram, key))
            if len(self._reorder_block) >= policy.reorder_window:
                self._flush_reorder_block()
        else:
            self._emit(datagram, key)

        if policy.duplication_pct > 0.0:
            roll = float(_rng(policy.seed, camera_id, frame_seq, "dup").uniform(0, 100))
            if roll < policy.duplication_pct:
                # A real retransmission: the SAME bytes on the wire again.
                self._emit(datagram, key)
                self.ledger.duplicated.append(key)

    def flush(self) -> None:
        """Release everything still held: delayed datagrams are late, not lost."""
        self._flush_reorder_block()
        for _, datagram, key in self._held:
            self._emit(datagram, key, count_position=False)
        self._held = []
