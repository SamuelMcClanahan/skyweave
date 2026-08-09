"""Background-model backends: fixed_bg, mog2 (cv2), ive_approx (numpy).

Each backend consumes PROC-resolution U8 luma frames in sequence order and
returns a boolean foreground mask. During the warm-up schedule the caller
still feeds frames (models must learn) but discards masks.

``mog2`` is the PRIMARY host baseline (Samuel's decision): OpenCV's
BackgroundSubtractorMOG2 with ``cv2.setNumThreads(0)`` so runs are
deterministic (test K2).

``ive_approx`` is a numpy Stauffer-Grimson GMM whose knobs mirror the
RK_MPI_IVE_GMM2 header controls. It predicts D8 board behavior by
scorecard comparison against mog2 — never bit parity (§7.3 principle).
"""

from __future__ import annotations

import numpy as np

from skyweave2.detector.components import _require_cv2
from skyweave2.detector.config import DetectorConfig


class FixedBgBackend:
    """Mean of the warm-up frames, frozen; |frame - bg| > threshold."""

    def __init__(self, config: DetectorConfig):
        self._config = config
        self._sum: np.ndarray | None = None
        self._count = 0
        self._background: np.ndarray | None = None

    def apply(self, frame: np.ndarray, warming_up: bool) -> np.ndarray:
        frame_f = frame.astype(np.float64)
        if warming_up:
            if self._sum is None:
                self._sum = np.zeros_like(frame_f)
            self._sum += frame_f
            self._count += 1
            return np.zeros(frame.shape, dtype=bool)
        if self._background is None:
            if self._count == 0:
                raise ValueError("fixed_bg needs at least one warm-up frame")
            self._background = self._sum / self._count
        return np.abs(frame_f - self._background) > self._config.fixed_bg.diff_threshold_dn


class Mog2Backend:
    """cv2 MOG2, single-threaded for determinism."""

    def __init__(self, config: DetectorConfig):
        cv2 = _require_cv2()
        cv2.setNumThreads(0)
        params = config.mog2
        self._subtractor = cv2.createBackgroundSubtractorMOG2(
            history=params.history,
            varThreshold=params.var_threshold,
            detectShadows=params.detect_shadows,
        )
        self._learning_rate = params.learning_rate

    def apply(self, frame: np.ndarray, warming_up: bool) -> np.ndarray:
        mask = self._subtractor.apply(frame, learningRate=self._learning_rate)
        if warming_up:
            return np.zeros(frame.shape, dtype=bool)
        return mask > 127  # MOG2 marks shadows 127; foreground 255


class IveApproxBackend:
    """Vectorized per-pixel GMM with IVE-GMM2-shaped knobs.

    State per pixel: ``model_num`` modes of (weight, mean, var). Update is
    classic Stauffer-Grimson with a single global learning rate (the IVE
    control), variance floored at ``var_min``. Background support = the
    highest-weight modes whose cumulative weight reaches ``bg_ratio``; a
    pixel is foreground when its matched mode is not in that set (or no
    mode matched).
    """

    def __init__(self, config: DetectorConfig):
        self._p = config.ive_approx
        self._weight: np.ndarray | None = None  # (K, H, W)
        self._mean: np.ndarray | None = None
        self._var: np.ndarray | None = None

    def _init_state(self, frame_f: np.ndarray) -> None:
        p = self._p
        shape = frame_f.shape
        self._weight = np.zeros((p.model_num, *shape), dtype=np.float32)
        # Unused modes start at an unmatchable sentinel mean: a mean of 0
        # would let dark pixels "match" a zero-weight phantom mode and dodge
        # the replacement rule.
        self._mean = np.full((p.model_num, *shape), -1.0e4, dtype=np.float32)
        self._var = np.full((p.model_num, *shape), p.var_init, dtype=np.float32)
        self._weight[0] = 1.0
        self._mean[0] = frame_f

    def apply(self, frame: np.ndarray, warming_up: bool) -> np.ndarray:
        p = self._p
        frame_f = frame.astype(np.float32)  # f32 state: full-res clips stay fast
        if self._weight is None:
            self._init_state(frame_f)
            return np.zeros(frame.shape, dtype=bool)
        weight, mean, var = self._weight, self._mean, self._var

        distance2 = (frame_f[None] - mean) ** 2
        matched = distance2 <= (p.match_sigmas**2) * var
        # Best matching mode per pixel: the matched mode with highest weight.
        match_score = np.where(matched, weight, -1.0)
        best = np.argmax(match_score, axis=0)  # (H, W)
        any_match = np.take_along_axis(matched, best[None], axis=0)[0]

        # Weight update: w += lr * (1[best & matched] - w).
        is_best = np.zeros_like(matched)
        np.put_along_axis(is_best, best[None], any_match[None], axis=0)
        weight += np.float32(p.learn_rate) * (is_best.astype(np.float32) - weight)

        # Matched-mode mean/var update.
        rho = p.learn_rate
        delta = frame_f[None] - mean
        mean += np.where(is_best, rho * delta, 0.0)
        var += np.where(is_best, rho * (delta**2 - var), 0.0)
        np.clip(var, p.var_min, None, out=var)

        # Unmatched pixels: replace the lowest-weight mode.
        no_match = ~any_match
        if np.any(no_match):
            lowest = np.argmin(weight, axis=0)
            replace = np.zeros_like(matched)
            np.put_along_axis(replace, lowest[None], no_match[None], axis=0)
            mean[replace] = np.broadcast_to(frame_f[None], mean.shape)[replace]
            var[replace] = p.var_init
            weight[replace] = p.weight_init

        weight /= weight.sum(axis=0, keepdims=True)

        # Background support: top modes by weight up to bg_ratio.
        order = np.argsort(-weight, axis=0)
        sorted_w = np.take_along_axis(weight, order, axis=0)
        cum = np.cumsum(sorted_w, axis=0)
        in_bg_sorted = (cum - sorted_w) < p.bg_ratio  # modes before the ratio is met
        in_bg = np.zeros_like(in_bg_sorted)
        np.put_along_axis(in_bg, order, in_bg_sorted, axis=0)

        best_in_bg = np.take_along_axis(in_bg, best[None], axis=0)[0]
        foreground = ~(any_match & best_in_bg)
        if warming_up:
            return np.zeros(frame.shape, dtype=bool)
        return foreground


def make_backend(config: DetectorConfig):
    from skyweave2.detector.config import Backend

    if config.backend == Backend.FIXED_BG:
        return FixedBgBackend(config)
    if config.backend == Backend.MOG2:
        return Mog2Backend(config)
    if config.backend == Backend.IVE_APPROX:
        return IveApproxBackend(config)
    raise ValueError(f"unknown backend {config.backend}")
