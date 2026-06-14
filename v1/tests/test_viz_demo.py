from skyweave.viz.demo import (
    MIN_SUPPORTING_CAMERAS,
    create_plane_track,
    create_demo_cameras,
    create_erratic_drone_track,
    create_scenario_tracks,
    create_demo_track,
    create_demo_weavefield,
    create_combined_weavefield,
    default_camera_array_for_scenario,
    distance_between,
    visible_camera_ids,
    voxel_center,
)


def test_demo_weavefield_requires_multi_camera_support() -> None:
    cameras = create_demo_cameras("perimeter-6")
    track = create_demo_track(1, frame=0, total_frames=600, supporting_camera_ids=[])

    weavefield = create_demo_weavefield(0, cameras, track["state"][:3], supporting_camera_ids=[])

    assert weavefield["voxels"] == []


def test_demo_voxels_are_ray_intersection_evidence_near_track() -> None:
    cameras = create_demo_cameras("perimeter-6")
    track = create_demo_track(1, frame=0, total_frames=600)
    supporting_ids = visible_camera_ids(cameras, track["state"][:3])
    assert len(supporting_ids) >= MIN_SUPPORTING_CAMERAS

    weavefield = create_demo_weavefield(0, cameras, track["state"][:3], supporting_ids)
    voxels = weavefield["voxels"]

    assert voxels
    assert weavefield["source_packet_ids"] == [f"cam{camera_id}" for camera_id in supporting_ids]
    best = voxels[0]
    best_center = voxel_center(best["ix"], best["iy"], best["iz"])
    assert distance_between(best_center, tuple(track["state"][:3])) < 35.0
    assert best["score"] > 2.0


def test_erratic_drone_is_slower_than_plane() -> None:
    drone = create_erratic_drone_track(1, frame=300, total_frames=1200)
    plane = create_plane_track(2, frame=300, total_frames=1200)

    drone_speed = sum(value * value for value in drone["state"][3:6]) ** 0.5
    plane_speed = sum(value * value for value in plane["state"][3:6]) ** 0.5

    assert 5.0 < drone_speed < 35.0
    assert plane_speed > drone_speed * 4.0
    assert plane["state"][0] > 6000.0
    assert plane["state"][1] < -16000.0
    assert plane["state"][2] > 1400.0


def test_airport_mixed_scenario_streams_drone_and_plane_evidence() -> None:
    cameras = create_demo_cameras("sfo-bay-10")
    tracks = create_scenario_tracks("airport-mixed", frame=300, total_frames=1200, cameras=cameras)
    weavefield = create_combined_weavefield(300, cameras, tracks)

    assert len(cameras) == 10
    assert [track["classification"] for track in tracks] == ["drone", "plane", "drone"]
    assert tracks[0]["visible_camera_ids"]
    assert tracks[1]["visible_camera_ids"]
    assert tracks[2]["visible_camera_ids"]
    assert weavefield["voxels"]
    assert len(weavefield["source_packet_ids"]) >= MIN_SUPPORTING_CAMERAS


def test_scenario_defaults_keep_local_drone_and_airport_layouts() -> None:
    assert default_camera_array_for_scenario("orbit-drone") == "perimeter-6"
    assert default_camera_array_for_scenario("erratic-drone-solo") == "sfo-bay-10"
    assert default_camera_array_for_scenario("high-altitude-plane") == "sfo-bay-10"
    assert default_camera_array_for_scenario("airport-mixed") == "sfo-bay-10"
