# AeroVis

Experimental multi-camera aircraft and drone tracking system.

The current repo contains:

- `Aerovision/SPEC_MVP.md` - draft MVP software specification.
- `Pixeltovoxelprojector/` - reference pixel-to-voxel projector checkout.
- `docs/conversations/` - design discussion notes and decision logs.

The immediate engineering goal is to validate calibrated camera geometry and
pixel-to-ray math before committing to a full runtime architecture. The current
direction is a hybrid:

- far aircraft: tiny moving-object detection, bearing/ray association, and
  probabilistic tracking;
- close drones: bounded local voxel scoring/reconstruction using calibrated
  foreground masks.

## Current Status

This is not yet an implementation repo. The next useful chunk is a synthetic
geometry testbed for camera projection, ray construction, voxel scoring, and
known ground-truth validation.

