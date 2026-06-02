from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SkyweaveModel(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)


class PacketHeader(SkyweaveModel):
    v: int = 1
    source_id: str
    source_type: Literal["camera", "edge", "turret", "replay", "synthetic"]
    frame_seq: int
    capture_ts_ns: int
    publish_ts_ns: int
    clock_domain: str = "mvp_host_monotonic"
    time_sync_error_ms: float | None = None


class MotionBlob(SkyweaveModel):
    blob_id: int
    cx: float
    cy: float
    bbox_x: int
    bbox_y: int
    bbox_w: int
    bbox_h: int
    area_px: int
    mean_diff: float
    max_diff: float
    confidence: float = Field(ge=0.0, le=1.0)
    local_track_id: int | None = None


class MotionPatch(SkyweaveModel):
    bbox_x: int
    bbox_y: int
    bbox_w: int
    bbox_h: int
    encoding: Literal["rle_u8", "png_gray", "sparse_xy"]
    payload: bytes
    value_scale: float = 1.0


class MotionPacket(SkyweaveModel):
    header: PacketHeader
    camera_id: int
    image_width: int
    image_height: int
    blobs: list[MotionBlob]
    motion_patches: list[MotionPatch] = []
    detector: str
    exposure_us: float | None = None
    gain_db: float | None = None


class Detection(SkyweaveModel):
    cx: float
    cy: float
    bbox_x: int
    bbox_y: int
    bbox_w: int
    bbox_h: int
    area_px: int
    confidence: float = Field(ge=0.0, le=1.0)
    local_track_id: int | None = None


class DetectionPacket(SkyweaveModel):
    header: PacketHeader
    camera_id: int
    detections: list[Detection]


class VoxelGridSpec(SkyweaveModel):
    frame_id: str
    origin: tuple[float, float, float]
    voxel_size_m: float
    dims: tuple[int, int, int]


class SparseVoxel(SkyweaveModel):
    ix: int
    iy: int
    iz: int
    score: float


class VoxelPeak(SkyweaveModel):
    position: tuple[float, float, float]
    score: float
    covariance: list[list[float]]
    supporting_camera_ids: list[int]
    n_voxels: int


class WeavefieldVolume(SkyweaveModel):
    ts_ns: int
    grid: VoxelGridSpec
    voxels: list[SparseVoxel]
    peaks: list[VoxelPeak]
    decay_s: float
    source_packet_ids: list[str]


class Measurement3D(SkyweaveModel):
    ts_ns: int
    source: Literal["voxel_peak", "triangulation", "turret", "synthetic"]
    position: tuple[float, float, float]
    covariance: list[list[float]]
    score: float
    supporting_camera_ids: list[int] = []


class Track(SkyweaveModel):
    id: int
    state: list[float]
    covariance: list[list[float]]
    status: Literal["candidate", "active", "coasting"]
    classification: str | None = None
    classification_confidence: float = 0.0
    created_ts_ns: int
    last_update_ts_ns: int
    update_count: int
    miss_count: int
    trail: list[tuple[float, float, float, int]]


class VizCamera(SkyweaveModel):
    id: int
    position: list[float]
    rotation_quat: list[float]
    fov_h_deg: float
    fov_v_deg: float
    fps: float
    online: bool


class VizFrame(SkyweaveModel):
    ts_ns: int
    tracks: list[Track]
    cameras: list[VizCamera]
    measurements: list[Measurement3D]
    weavefield_history: list[WeavefieldVolume]
    stats: dict[str, float]


class RunSummary(SkyweaveModel):
    scene: str
    frames: int
    voxel_size_m: float
    peak_rmse_m: float
    track_rmse_m: float
    max_track_error_m: float
    latency_p50_ms: float
    latency_p95_ms: float
    dropped_packets: int
    false_positive_packets: int
    not_visible_packets: int = 0
    passed: bool
