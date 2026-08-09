"""Base model for all contract types.

- extra="ignore": unknown fields from a newer peer are dropped, not fatal (T5).
- frozen models where mutation would break an invariant (FrameEnvelope).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class FrozenContractModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
