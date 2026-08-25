"""Connected-component centroid semantics and overlap diagnostics."""

from __future__ import annotations

import numpy as np

from skyweave2.detector.components import (
    count_overlapping_bbox_pairs,
    find_components,
)


def test_nested_bbox_uses_all_foreground_for_moment_but_keeps_component_area():
    mask = np.zeros((9, 9), dtype=bool)
    # A hollow 7x7 perimeter (area 24, own centroid exactly (4, 4)).
    mask[1, 1:8] = True
    mask[7, 1:8] = True
    mask[2:7, 1] = True
    mask[2:7, 7] = True
    # A disconnected one-pixel component nested inside its bbox, off-centre.
    mask[4, 3] = True

    components = find_components(mask, min_area_px=1, max_area_px=100)
    outer = next(component for component in components if component.area_px == 24)
    inner = next(component for component in components if component.area_px == 1)

    assert outer.area_px == 24  # never replaced by the 25-pixel moment support
    assert outer.centroid_u == 99 / 25
    assert outer.centroid_v == 4.0
    assert (inner.centroid_u, inner.centroid_v) == (3.0, 4.0)
    assert count_overlapping_bbox_pairs(components) == 1


def test_crossing_bboxes_count_one_unordered_pair_and_share_mask_pixels():
    mask = np.zeros((7, 7), dtype=bool)
    # Bottom-left L, bbox [0, 5) x [0, 5).
    mask[0:5, 0] = True
    mask[4, 1:5] = True
    # Top-right inverted L, bbox [2, 7) x [2, 7).  The shapes never touch,
    # while their boxes overlap over [2, 5) x [2, 5).
    mask[2, 2:7] = True
    mask[3:7, 6] = True

    components = find_components(mask, min_area_px=1, max_area_px=100)
    by_left = {component.bbox_x: component for component in components}

    assert len(components) == 2
    assert count_overlapping_bbox_pairs(components) == 1
    assert by_left[0].area_px == by_left[2].area_px == 9
    assert (by_left[0].centroid_u, by_left[0].centroid_v) == (19 / 12, 32 / 12)
    assert (by_left[2].centroid_u, by_left[2].centroid_v) == (53 / 12, 40 / 12)


def test_nonoverlapping_bboxes_preserve_component_moments_and_count_zero():
    mask = np.zeros((9, 10), dtype=bool)
    mask[1:3, 1:3] = True
    mask[6:8, 7:9] = True

    components = find_components(mask, min_area_px=1, max_area_px=100)

    assert [(component.centroid_u, component.centroid_v) for component in components] == [
        (1.5, 1.5),
        (7.5, 6.5),
    ]
    assert count_overlapping_bbox_pairs(components) == 0


def test_overlap_counter_only_considers_area_accepted_components():
    mask = np.zeros((9, 9), dtype=bool)
    mask[1, 1:8] = True
    mask[7, 1:8] = True
    mask[2:7, 1] = True
    mask[2:7, 7] = True
    mask[4, 3] = True

    components = find_components(mask, min_area_px=2, max_area_px=100)

    assert len(components) == 1
    # The rejected pixel remains part of the final binary-mask moment, but it
    # cannot form an accepted-component overlap pair.
    assert components[0].centroid_u == 99 / 25
    assert count_overlapping_bbox_pairs(components) == 0
