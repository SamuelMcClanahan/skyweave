from __future__ import annotations

import struct

import numpy as np


def encode_rle_u8(mask: np.ndarray) -> bytes:
    flat = mask.astype(np.uint8, copy=False).ravel()
    if flat.size == 0:
        return b""
    chunks = []
    current = int(flat[0])
    count = 0
    for value in flat:
        value_i = int(value)
        if value_i == current and count < 65535:
            count += 1
            continue
        chunks.append(struct.pack("<HB", count, current))
        current = value_i
        count = 1
    chunks.append(struct.pack("<HB", count, current))
    return b"".join(chunks)


def decode_rle_u8(payload: bytes, width: int, height: int) -> np.ndarray:
    values: list[int] = []
    for offset in range(0, len(payload), 3):
        count, value = struct.unpack("<HB", payload[offset : offset + 3])
        values.extend([value] * count)
    arr = np.asarray(values, dtype=np.uint8)
    expected = width * height
    if arr.size != expected:
        raise ValueError(f"Decoded RLE size {arr.size} does not match expected {expected}")
    return arr.reshape((height, width))

