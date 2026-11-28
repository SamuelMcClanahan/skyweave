"""ONNX Runtime CPU backend for percepd v1 (B-D2).

``onnxruntime`` is the optional extra ``percepd`` (``v2/pyproject.toml``). It
is imported inside :class:`OnnxBackend`, never at module import, so the rest
of the drone stack and its tests run without it.

The CPU backend proves the chain camera -> detector -> boxes -> tracker. Its
speed is not the flight number (B-D2); the NPU backend comes later.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from skyweave2.drone.percepd.detect import BackendError, DetectConfig, Detection, postprocess
from skyweave2.drone.percepd.geometry import letterbox_fit
from skyweave2.drone.percepd.layouts import ModelLayout, make_blob

log = logging.getLogger(__name__)


class OnnxBackend:
    """One ONNX model on the CPU. Input ``(1, 3, H, W)`` float32 with a fixed
    ``H`` and ``W``; the first output is decoded by ``layout``."""

    def __init__(
        self,
        model_path: str | Path,
        layout: ModelLayout,
        config: DetectConfig,
        *,
        threads: int = 0,
    ) -> None:
        try:
            import onnxruntime as ort
        except ImportError as exc:
            raise BackendError(
                "the ONNX backend needs onnxruntime: install the extra, "
                "for example `uv sync --extra percepd` or `pip install onnxruntime`"
            ) from exc
        path = Path(model_path)
        if not path.is_file():
            raise BackendError(f"model file not found: {path} (see percepd/README.md)")
        opts = ort.SessionOptions()
        if threads > 0:
            opts.intra_op_num_threads = threads
        try:
            self._session = ort.InferenceSession(
                str(path), sess_options=opts, providers=["CPUExecutionProvider"]
            )
        except Exception as exc:  # noqa: BLE001 - onnxruntime raises its own error types
            raise BackendError(f"cannot load {path}: {exc}") from exc
        inp = self._session.get_inputs()[0]
        shape = inp.shape
        if (
            len(shape) != 4
            or shape[1] != 3
            or not isinstance(shape[2], int)
            or not isinstance(shape[3], int)
            or inp.type != "tensor(float)"
        ):
            raise BackendError(
                f"model input must be float32 (1, 3, H, W) with fixed H and W, "
                f"got {inp.type} {shape}"
            )
        self._input_name = inp.name
        self.in_h, self.in_w = int(shape[2]), int(shape[3])
        self.layout = layout
        self.config = config
        log.info(
            "onnx backend: %s, input %dx%d, layout %s, onnxruntime %s",
            path.name,
            self.in_w,
            self.in_h,
            layout.name,
            ort.__version__,
        )

    def detect(self, frame: np.ndarray) -> list[Detection]:
        lb = letterbox_fit(
            frame.shape[1], frame.shape[0], self.in_w, self.in_h, center=self.layout.center_pad
        )
        blob = make_blob(frame, lb, self.layout)
        output = self._session.run(None, {self._input_name: blob})[0]
        return postprocess(output, self.layout, lb, self.config)
