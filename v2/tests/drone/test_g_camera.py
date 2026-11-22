"""G series, camera side: mount and lens from config, lens distortion.

Contract: DRONE_CONTRACTS_D0.md [C3] (configured mount, V-D1), [G1] and [G2]
(the undistorted box, V-D11), [R2] (the camera in ``meta.config``), §9 (the
mounts and lenses), implementing §11.1 V-D1, V-D11 and §11.2 E2-D2, E2-D6.

Expected values are derived by hand in each docstring from the contract
formulas and the E2-D2 numbers (``cone_up`` R_body_cam, f = 806 px, cx =
959.5, cy = 599.5), or come from the detection D0 camera
(``contracts/camera.py``), whose Brown-Conrady convention the drone camera
shares. The real camera model, synthetic camera, guidance functions, core and
recorder run; nothing is mocked.
"""

from __future__ import annotations

import io
import math

import numpy as np
import pytest

from skyweave2.contracts.camera import CameraModel as D0Camera
from skyweave2.drone.camera import (
    CONE_UP_HFOV100,
    LENSES,
    MOUNTS,
    CameraModel,
)
from skyweave2.drone.core import CompanionCore, CoreConfig, replay
from skyweave2.drone.guidance import GuidanceConfig, derotate, pinhole_box, size_range
from skyweave2.drone.harness.camera_sim import CameraSimConfig, Pose, SyntheticCamera
from skyweave2.drone.harness.targets import StaticBalloon
from skyweave2.drone.packets import TrackPacket, TrackState
from skyweave2.drone.recording import Recorder, Stream, read_records
from skyweave2.drone.types import Attitude

DEG = math.pi / 180.0
CX, CY = 959.5, 599.5
LEVEL = Attitude(t_ms=0, time_boot_ms=0, roll=0.0, pitch=0.0, yaw=0.0)
HOVER = Pose(pos_ned=(0.0, 0.0, -10.0))
CLEAN = CameraSimConfig()  # no noise, no dropout, no boresight error
K1 = (-0.3, 0.0, 0.0, 0.0, 0.0)  # the brief's E2a-2 placeholder, Provisional
FULL = (-0.3, 0.1, 1e-3, -1e-3, 0.0)  # every term on; no fold (1 - 0.9 s + 0.5 s^2 > 0)


def _balloon(az_deg: float, el_deg: float, d: float = 20.0) -> StaticBalloon:
    """A 1 m balloon ``d`` m from HOVER at azimuth / elevation (NED, level vehicle)."""
    az, el = az_deg * DEG, el_deg * DEG
    ned = (
        d * math.cos(el) * math.cos(az),
        d * math.cos(el) * math.sin(az),
        -10.0 - d * math.sin(el),
    )
    return StaticBalloon(anchor_ned=ned, name="target", width_m=1.0, height_m=1.0)


def _track(u: float, v: float, w: float, h: float) -> TrackPacket:
    return TrackPacket(
        t_cap=0, track_id=1, state=TrackState.CONFIRMED, u=u, v_px=v, du=0.0, dv=0.0,
        w=w, h=h, hits=5, misses=0, age_frames=5,
    )  # fmt: skip


# ---------------------------------------------------------------------------
# V-D1, E2-D2: named mounts and lenses
# ---------------------------------------------------------------------------


def test_c3_v_d1_named_mounts_and_lenses() -> None:
    """[C3], V-D1, E2-D2, §9: ``c3_forward`` is the E1 [C3] matrix and ``cone_up`` is
    E2-D2's; ``e1_f1000`` is f = 1000 and ``hfov100`` is f = 806 (960 / tan 50 deg =
    805.5), both centered. The default camera is the E1 one. A named mount or lens
    fixes its numbers; another name is a measured or custom one."""
    assert MOUNTS["c3_forward"] == ((0, 0, 1), (1, 0, 0), (0, 1, 0))
    assert MOUNTS["cone_up"] == ((1, 0, 0), (0, -1, 0), (0, 0, -1))
    assert LENSES == {"e1_f1000": (1000.0, CX, CY), "hfov100": (806.0, CX, CY)}
    e1 = CameraModel()
    assert (e1.mount, e1.lens, e1.dist, e1.f_px) == ("c3_forward", "e1_f1000", (0.0,) * 5, 1000.0)
    assert CameraModel.named() == e1
    assert (CONE_UP_HFOV100.mount, CONE_UP_HFOV100.lens) == ("cone_up", "hfov100")
    assert CONE_UP_HFOV100.r_body_cam == MOUNTS["cone_up"] and CONE_UP_HFOV100.f_px == 806.0
    with pytest.raises(ValueError):
        CameraModel(r_body_cam=MOUNTS["cone_up"])  # named c3_forward, other matrix
    with pytest.raises(ValueError):
        CameraModel(f_px=806.0)  # named e1_f1000, other f
    with pytest.raises(ValueError):
        CameraModel.named("cone_down")
    with pytest.raises(ValueError):
        CameraModel(dist=(0.1, 0.0, 0.0, 0.0))
    measured = CameraModel(f_px=812.3, lens="cal_2026_12", r_body_cam=MOUNTS["cone_up"], mount="m1")
    assert (measured.lens, measured.mount) == ("cal_2026_12", "m1")


def test_e2_d2_cone_up_target_straight_above_lands_at_the_center() -> None:
    """E2-D2: the optical axis is body -z, so a target straight above a level vehicle
    projects to (cx, cy) at any yaw. Through the synthetic camera it is in view with
    a centered box of w = 806 * 1 / 10 = 80.6 px."""
    for yaw in (0.0, 1.0, -2.5):
        att = Attitude(t_ms=0, time_boot_ms=0, roll=0.0, pitch=0.0, yaw=yaw)
        assert CONE_UP_HFOV100.project_ned((0.0, 0.0, -10.0), att) == pytest.approx((CX, CY))
    sim = SyntheticCamera(CLEAN, seed=1, nominal=CONE_UP_HFOV100)
    up = StaticBalloon(anchor_ned=(0.0, 0.0, -20.0), name="target", width_m=1.0, height_m=1.0)
    fr = sim.capture(0, HOVER, [up])
    (box,) = fr.packet.boxes
    assert box.center == pytest.approx((CX, CY)) and box.w == pytest.approx(80.6)
    assert fr.truth.get("target").in_fov


def test_e2_d2_e2_d6_cone_up_sees_40_deg_up_along_body_x_only() -> None:
    """E2-D2 (u along body x, v along body -y), E2-D6 (a level vehicle sees targets
    40 deg or more above its horizon along u): at elevation e along body +x the
    camera ray is (cot e, 0, 1), so u = 959.5 + 806 cot e.

    Hand: e = 40 -> u = 959.5 + 960.553 = 1920.053, 0.55 px past the u edge 1919.5
    ([C2]; f = 806 rounds 805.5 up). Aft (-x) mirrors it: u = -1.053. e = 41 -> u =
    959.5 + 927.197 = 1886.70, in view; e = 39 -> u = 1954.83, out of view. Along body
    +y, v = 599.5 - 806 cot e: e = 40 -> v = -361.05, out of view (no box); e = 54 ->
    v = 13.91, in view (the v half-angle is atan(600 / 806) = 36.7 deg, so 53.3 deg).
    """
    cam = CONE_UP_HFOV100
    fwd = cam.project_ned((math.cos(40 * DEG), 0.0, -math.sin(40 * DEG)), LEVEL)
    aft = cam.project_ned((-math.cos(40 * DEG), 0.0, -math.sin(40 * DEG)), LEVEL)
    assert fwd == pytest.approx((1920.053, CY), abs=1e-3)
    assert aft == pytest.approx((-1.053, CY), abs=1e-3)
    right = cam.project_ned((0.0, math.cos(40 * DEG), -math.sin(40 * DEG)), LEVEL)
    assert right == pytest.approx((CX, -361.053), abs=1e-3)
    sim = SyntheticCamera(CLEAN, seed=1, nominal=cam)

    def seen(az: float, el: float) -> bool:
        return sim.capture(0, HOVER, [_balloon(az, el)]).truth.get("target").in_fov

    assert seen(0.0, 41.0) and seen(180.0, 41.0)
    assert not seen(0.0, 39.0) and not seen(180.0, 39.0)
    assert not seen(90.0, 40.0) and not seen(-90.0, 40.0)
    assert seen(90.0, 54.0) and seen(-90.0, 54.0)
    assert sim.capture(0, HOVER, [_balloon(90.0, 40.0)]).packet.boxes == ()


def test_r2_v_d1_camera_choice_round_trips_through_the_recording() -> None:
    """[R2], V-D1, V-D11: the companion config names the mount, lens and distortion;
    the recording's meta.config carries them; a core rebuilt from it equals the
    original, and the recording replays ([R3]). An E1 recording's camera object (no
    dist, mount, lens) reads as the E1 camera, and only with the E1 numbers."""
    cam = CameraModel.named("cone_up", "hfov100", dist=K1)
    config = CoreConfig(camera=cam)
    buf = io.StringIO()
    core = CompanionCore(config, recorder=Recorder(buf), t_start_ms=100)
    for t in (150, 200, 250):
        core.on_tick(t)
    lines = buf.getvalue().splitlines()
    (meta, *_rest) = read_records(lines)
    assert meta.stream is Stream.META
    assert meta.config["camera"]["mount"] == "cone_up"
    assert meta.config["camera"]["lens"] == "hfov100"
    assert meta.config["camera"]["dist"] == [-0.3, 0.0, 0.0, 0.0, 0.0]
    assert CoreConfig.from_obj(meta.config) == config
    assert replay(lines).matches
    e1_obj = {k: v for k, v in CameraModel().to_obj().items() if k not in ("dist", "mount", "lens")}
    assert CameraModel.from_obj(e1_obj) == CameraModel()
    with pytest.raises(ValueError):
        CameraModel.from_obj({**e1_obj, "f_px": 806.0})


# ---------------------------------------------------------------------------
# V-D11: Brown-Conrady distortion
# ---------------------------------------------------------------------------


def test_v_d11_distortion_is_the_detection_d0_convention() -> None:
    """V-D11 ("one ChArUco calibration serves both lines"): with the same
    coefficients, the drone camera's distortion of normalized coordinates equals the
    detection D0 camera's (``contracts/camera.py``), bit for bit, on a grid."""
    d0 = D0Camera(
        camera_id=0, width=1920, height=1200,
        k=((1000.0, 0.0, CX), (0.0, 1000.0, CY), (0.0, 0.0, 1.0)),
        dist=FULL,
        t_world_cam=tuple(tuple(float(i == j) for j in range(4)) for i in range(4)),
        calibration_rev="test",
    )  # fmt: skip
    cam = CameraModel(dist=FULL)
    for x in np.linspace(-1.0, 1.0, 9).tolist():
        for y in np.linspace(-0.6, 0.6, 7).tolist():
            assert cam.distort_normalized(x, y) == d0.distort_normalized(x, y)


@pytest.mark.parametrize("lens", ["e1_f1000", "hfov100"])
def test_v_d11_round_trip_on_a_grid_inside_the_frame(lens: str) -> None:
    """V-D11, brief E2a-2 gate: raw pixel -> undistort -> distort returns the pixel
    (1e-6 px) on a 25 x 16 grid over the frame, and pinhole -> distort -> undistort
    returns it too.

    With every term on (FULL) the radial term never folds, so every grid point
    inverts. With k1 = -0.3 alone, ``r_d = r (1 - 0.3 r^2)`` peaks at r = 1/sqrt(0.9)
    = 1.054093 (the fold radius) with r_d = 2/3 of that = 0.702728. A raw pixel
    farther than 0.702728 f from the center has no inverse (NaN); one inside 0.999
    of it inverts. In pixels that is 702.7 px for f = 1000 and 566.4 px for f = 806,
    inside the frame (its u half-width is 960 px): the placeholder lens does not
    image the frame's sides and corners. Logged as a finding, not built around.
    """
    for dist in (FULL, K1):
        cam = CameraModel.named(lens=lens, dist=dist)
        r_raw_max = 2.0 / 3.0 * cam.fold_radius * cam.f_px
        if dist == K1:
            assert cam.fold_radius == pytest.approx(1.0540926, abs=1e-7)
        else:
            assert cam.fold_radius == math.inf
        n_in = 0
        for u in np.linspace(-0.5, 1919.5, 25):
            for v in np.linspace(-0.5, 1199.5, 16):
                r = math.hypot(u - CX, v - CY)
                up, vp = cam.undistort_px(float(u), float(v))
                if r > r_raw_max:
                    assert math.isnan(up) and math.isnan(vp)
                    continue
                if r < 0.999 * r_raw_max:
                    assert cam.distort_px(up, vp) == pytest.approx((u, v), abs=1e-6)
                    assert cam.undistort_px(*cam.distort_px(up, vp)) == pytest.approx(
                        (up, vp), abs=1e-6
                    )
                    n_in += 1
        if dist == FULL:
            assert n_in == 25 * 16
        else:
            assert n_in > 100


def test_v_d11_zero_coefficients_change_nothing() -> None:
    """V-D11 ("default all zero"; brief: "with zero coefficients the E1 fingerprint is
    unchanged"): with no distortion every distortion function returns its input
    unchanged and guidance reads the raw box as is. (The E1 fingerprint itself is
    pinned by ``test_s11_s12_gate_worlds_and_e1_camera_output_are_unchanged``.)"""
    for cam in (CameraModel(), CONE_UP_HFOV100):
        assert not cam.distorted and cam.fold_radius == math.inf
        assert cam.distort_px(12.25, -3.5) == (12.25, -3.5)
        assert cam.undistort_px(12.25, -3.5) == (12.25, -3.5)
        assert cam.distort_box(1500.0, 20.0, 33.0, 44.0) == (1500.0, 20.0, 33.0, 44.0)
        assert cam.undistort_box(1500.0, 20.0, 33.0, 44.0) == (1500.0, 20.0, 33.0, 44.0)
        assert pinhole_box(_track(1500.0, 20.0, 33.0, 44.0), cam) == (1500.0, 20.0, 33.0)


def test_v_d11_box_rule_and_its_inverse() -> None:
    """V-D11 box rule: the center is distorted; the width is the raw distance between
    the distorted midpoints of the left and right edges (the height likewise).

    Hand, k1 = -0.3, f = 1000, a centered 800 x 800 box: the edge midpoints sit at
    x = +-0.4, so the raw half-width is 0.4 (1 - 0.3 * 0.16) = 0.3808 -> 761.6 px; the
    inverse gives 800 back. Off center (40 deg right, 20 m), the inverse also returns
    the pinhole box (1e-6 px).
    """
    cam = CameraModel(dist=K1)
    assert cam.distort_box(CX, CY, 800.0, 800.0) == pytest.approx((CX, CY, 761.6, 761.6))
    assert cam.undistort_box(CX, CY, 761.6, 761.6) == pytest.approx((CX, CY, 800.0, 800.0))
    pin = (CX + 1000.0 * math.tan(40 * DEG), CY - 150.0, 65.0, 50.0)
    assert cam.undistort_box(*cam.distort_box(*pin)) == pytest.approx(pin, abs=1e-6)


def test_g1_g2_v_d11_guidance_undistorts_the_box_before_use() -> None:
    """[G1], [G2], V-D11: a 1 m balloon 20 m away at azimuth / elevation (40, 0),
    (30, 15), (35, 20) and (20, 25) deg, rendered by the synthetic camera with k1 =
    -0.3 (no noise), gives back its true line of sight (1e-9) and range (1e-6 m) in
    guidance. The truth is the scene the test puts in. Discrimination: the same raw
    packets read as a pinhole camera are off by more than 2.5 deg (hand: at 40 deg
    the raw radius is tan 40 (1 - 0.3 tan^2 40) = 0.66186, atan 0.66186 = 33.5 deg).
    Past the fold (50 deg off the axis) the sim gives no box and no truth center."""
    cam = CameraModel(dist=K1)
    sim = SyntheticCamera(CLEAN, seed=1, nominal=cam)
    cfg = GuidanceConfig()
    for az, el in ((40.0, 0.0), (30.0, 15.0), (35.0, 20.0), (20.0, 25.0)):
        fr = sim.capture(0, HOVER, [_balloon(az, el)])
        (b,) = fr.packet.boxes
        assert fr.truth.get("target").center_px == pytest.approx(b.center)
        pkt = _track(b.center[0], b.center[1], b.w, b.h)
        want = np.array(
            [math.cos(el * DEG) * math.cos(az * DEG),
             math.cos(el * DEG) * math.sin(az * DEG),
             -math.sin(el * DEG)]
        )  # fmt: skip
        assert np.allclose(derotate(pkt, cam, LEVEL), want, atol=1e-9)
        assert size_range(pkt, cam, 1.0, cfg)[1] == pytest.approx(20.0, abs=1e-6)
        pinhole_los = derotate(pkt, CameraModel(), LEVEL)
        assert math.acos(float(np.clip(pinhole_los @ want, -1.0, 1.0))) > 2.5 * DEG
    fr = sim.capture(0, HOVER, [_balloon(50.0, 0.0)])
    assert fr.packet.boxes == () and fr.truth.get("target").center_px is None
