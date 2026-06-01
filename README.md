# Skyweave

Experimental multi-camera aircraft and drone tracking system.

The current repo contains:

- `SPEC_MVP.md` - draft Skyweave MVP software specification.
- `Pixeltovoxelprojector/` - reference pixel-to-voxel projector checkout.
- `docs/conversations/` - design discussion notes and decision logs.

The immediate engineering goal is to validate calibrated camera geometry and
pixel-to-ray math before committing to a full runtime architecture. The current
direction is Skyweave:

- **Rayweave**: calibrated 2D motion evidence projected into 3D voxel evidence;
- **Weavefield**: sparse 4D evidence history for visualization, replay, and
  tracking;
- **Tracking**: voxel peaks and triangulation baselines filtered into stable
  object tracks.

## Current Status

This is not yet an implementation repo. The next useful chunk is a synthetic
geometry testbed for camera projection, ray construction, voxel scoring, and
known ground-truth validation.
