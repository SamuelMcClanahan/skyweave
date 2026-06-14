from __future__ import annotations

from typing import TypeVar

import msgpack

from skyweave.messages import SkyweaveModel

T = TypeVar("T", bound=SkyweaveModel)


def pack_model(model: SkyweaveModel) -> bytes:
    return msgpack.packb(model.model_dump(mode="python"), use_bin_type=True)


def unpack_model(data: bytes, model_type: type[T]) -> T:
    payload = msgpack.unpackb(data, raw=False)
    return model_type.model_validate(payload)

