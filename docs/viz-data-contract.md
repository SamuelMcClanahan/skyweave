# Visualization Data Contract

This contract keeps Three.js and the Skyweave backend coupled through exported
data, not scorer internals.

## Bundle Layout

`skyweave-viz-export` writes one directory per bundle:

```text
manifest.json
grid.json
cameras.json
frames.jsonl
weavefields.jsonl
measurements.jsonl
tracks.jsonl
summary.json
```

JSONL files contain one complete JSON object per line.

## Static Files

`grid.json` is a `VoxelGridSpec`:

- `frame_id`
- `origin`
- `voxel_size_m`
- `dims`

`cameras.json` is an array of camera records. Each record contains:

- `viz`: the compact `VizCamera` payload for rendering
- `image_width`
- `image_height`
- `intrinsics`
- `distortion`
- `T_world_cam`

`VizCamera.rotation_quat` is `[x, y, z, w]` and represents the camera-to-world
rotation from `T_world_cam`.

## Frame Stream

Each line in `frames.jsonl` is a `VizFrame`:

- `frame_seq`
- `ts_ns`
- `tracks`
- `cameras`
- `measurements`
- `weavefield_history`
- `truth_position`
- `stats`

`weavefield_history` currently contains the latest sparse `WeavefieldVolume`
only. The visualizer should treat it as a list so longer history can be added
without changing the top-level shape.

## Sparse Weavefield

Each `WeavefieldVolume.voxels` entry is:

- `ix`
- `iy`
- `iz`
- `score`

The visualizer converts voxel indices to world centers with:

```text
center = origin + ([ix, iy, iz] + 0.5) * voxel_size_m
```

Do not rely on dense scorer arrays in frontend code.

## Runtime Rule

The visualizer is a consumer. It should read `VizFrame`, `WeavefieldVolume`,
`Measurement3D`, `Track`, camera metadata, and grid metadata only. It should not
import or mirror Rayweave scorer details.
