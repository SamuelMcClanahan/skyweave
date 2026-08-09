"""msgpack serialization for contract models (D0).

Interim wire format until the D7 protobuf freeze. Deterministic: two packs
of an equal model produce identical bytes (keys are sorted).
"""

from __future__ import annotations

from typing import TypeVar

import msgpack
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


def _sort_keys(value: object) -> object:
    if isinstance(value, dict):
        return {key: _sort_keys(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_sort_keys(item) for item in value]
    return value


def pack(model: BaseModel) -> bytes:
    payload = _sort_keys(model.model_dump(mode="json"))
    return msgpack.packb(payload, use_bin_type=True)


def unpack(data: bytes, model_type: type[T]) -> T:
    payload = msgpack.unpackb(data, raw=False)
    return model_type.model_validate(payload)
