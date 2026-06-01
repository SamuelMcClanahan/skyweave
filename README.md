# Skyweave

Experimental multi-camera aircraft and drone tracking system.

The current repo contains:

- `SPEC_MVP.md` - draft Skyweave MVP software specification.
- `Pixeltovoxelprojector/` - reference pixel-to-voxel projector checkout.
- `docs/conversations/` - design discussion notes and decision logs.

- **Rayweave**: calibrated 2D motion evidence projected into 3D voxel evidence;
- **Weavefield**: sparse 4D evidence history for visualization, replay, and
  tracking;
- **Tracking**: voxel peaks and triangulation baselines filtered into stable
  object tracks.
