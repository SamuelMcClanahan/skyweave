"""[S7] gate and probe seeds for the closed-loop scenarios (contract §8).

The gate seeds are fixed by a rule, not picked from a list. Anyone can
recompute them from the scenario label alone, so nobody can choose seeds
after seeing results:

    seed_i = first 4 bytes, big-endian, of sha256("E1-gate:<scenario>:<i>")

for ``i < gate_seed_count`` (20 for S2, 3 for the others, contract §9). Probe
seeds use the same rule in the ``E1-probe`` namespace. Development,
debugging, and any tuning use probe seeds only, and every scorecard names
its seed set. :func:`check_seed` refuses a scorecard whose seed does not
belong to the set it claims, and it refuses a gate seed labelled as a probe.
"""

from __future__ import annotations

import hashlib
from enum import Enum

SCENARIOS: tuple[str, ...] = ("S1", "S2", "S3", "S4", "S5", "S6")
GATE_NAMESPACE = "E1-gate"
PROBE_NAMESPACE = "E1-probe"
GATE_SEED_COUNT_S2 = 20  # contract §9 gate_seed_count (E1), Provisional
GATE_SEED_COUNT_OTHER = 3
GATE_REPEATS = 2  # contract §9 gate_repeats (E1), [S0]


class SeedSet(str, Enum):
    """The seed set a scorecard names ([S7])."""

    GATE = "gate"
    PROBE = "probe"


def _check_scenario(scenario: str) -> None:
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario {scenario!r}; known: {', '.join(SCENARIOS)}")


def seed_from_label(label: str) -> int:
    """First 4 bytes, big-endian, of ``sha256(label)`` (ASCII label)."""
    return int.from_bytes(hashlib.sha256(label.encode("ascii")).digest()[:4], "big")


def seed_for(seed_set: SeedSet, scenario: str, i: int) -> int:
    """[S7] seed ``i`` of ``scenario`` in ``seed_set``'s namespace."""
    _check_scenario(scenario)
    if isinstance(i, bool) or not isinstance(i, int) or i < 0:
        raise ValueError(f"seed index must be an int >= 0, got {i!r}")
    namespace = GATE_NAMESPACE if SeedSet(seed_set) is SeedSet.GATE else PROBE_NAMESPACE
    return seed_from_label(f"{namespace}:{scenario}:{i}")


def gate_seed_count(scenario: str) -> int:
    """Contract §9 ``gate_seed_count``: 20 for S2, 3 for the others."""
    _check_scenario(scenario)
    return GATE_SEED_COUNT_S2 if scenario == "S2" else GATE_SEED_COUNT_OTHER


def gate_seeds(scenario: str) -> tuple[int, ...]:
    """The scenario's gate seeds, in index order."""
    return tuple(seed_for(SeedSet.GATE, scenario, i) for i in range(gate_seed_count(scenario)))


def probe_seeds(scenario: str, count: int) -> tuple[int, ...]:
    """The first ``count`` probe seeds of the scenario (development and tuning)."""
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError(f"probe seed count must be an int >= 0, got {count!r}")
    return tuple(seed_for(SeedSet.PROBE, scenario, i) for i in range(count))


def check_seed(seed_set: SeedSet | str, scenario: str, seed: int) -> SeedSet:
    """Refuse a seed that does not belong to the set it is labelled with.

    A gate seed must be one of the scenario's gate seeds. A probe seed must
    not be any scenario's gate seed (tuning on a gate seed under a probe
    label is the leak [S7] forbids). Returns the parsed seed set.
    """
    parsed = SeedSet(seed_set)
    _check_scenario(scenario)
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**32:
        raise ValueError(f"seed must be a 32-bit unsigned int, got {seed!r}")
    if parsed is SeedSet.GATE:
        if seed not in gate_seeds(scenario):
            raise ValueError(f"seed {seed} is not a gate seed of {scenario} ([S7])")
    elif any(seed in gate_seeds(s) for s in SCENARIOS):
        raise ValueError(f"seed {seed} is a gate seed; it cannot be used as a probe ([S7])")
    return parsed
