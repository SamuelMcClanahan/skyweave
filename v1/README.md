Skyweave

Experimental multi-camera 3D tracking system for small aircraft, drones, and
other moving objects.

Purpose

Skyweave explores whether sparse evidence from many calibrated cameras can be
fused into a shared 3D world model without transmitting full video from every
sensor. The current repository is an MVP and research codebase, not production
software.

Core idea

Each camera detects motion locally.
Image evidence is converted into rays using camera intrinsics and extrinsics.
Rayweave scores voxel support across cameras.
Voxel peaks become 3D measurements.
A Kalman tracker estimates position and velocity.
The operator UI and visualizer expose the result for live tuning, recording,
and demos.

Current capabilities

Synthetic packet simulation for fast core validation.
Rendered synthetic frame simulation through the same frame-diff path as real
cameras.
Scalable synthetic camera arrays.
Pixel-plane demo for small image-space targets seen by dispersed cameras.
Live UVC camera capture and ChArUco-based calibration tools.
Motion fragment merging, centroid evidence scoring, and Kalman gating.
Operator web UI with camera previews, tuning controls, recordings, and
profiles.
Three.js/Cesium visualizer for cameras, frustums, rays, voxels, tracks, trails,
and room meshes.
Recording and replay support.

MVP hardware

Rubik Pi 3.
Three OV9281 UVC grayscale cameras.
Printed ChArUco board for intrinsics and fixed-layout pose solving.
Mac or desktop machine for heavier synthetic demos and visualization.

Repository layout

configs contains simulation, camera, calibration, and demo profiles.
src/skyweave/camera contains capture and motion extraction.
src/skyweave/calibration contains ChArUco detection, intrinsics, and extrinsics.
src/skyweave/rayweave contains ray scoring and voxel peak extraction.
src/skyweave/fusion contains triangulation and Kalman tracking.
src/skyweave/sim contains analytic and rendered synthetic sources.
src/skyweave/operator contains the live control server and runtime.
src/skyweave/viz contains visualization server and data contract helpers.
viz_web contains the browser visualizer.
tests contains regression and smoke coverage.
docs contains supplemental specs and notes.

Minimal local check

Create a Python virtual environment, install the package in editable mode with
development extras, then run pytest. The test suite covers configuration
parsing, calibration helpers, motion extraction, synthetic pipelines, operator
settings, Rayweave scoring, and visualizer data paths.

Development posture

Keep calibration files and recorded data separate from default code paths.
Prefer OpenCV implementations where they match the intended operation.
Keep synthetic and real pipelines using the same packet, scoring, and tracking
interfaces.
Treat the browser visualizer as a demo surface, not the core fusion engine.
Benchmark target hardware before optimizing broad areas of the codebase.

Detailed setup

Detailed command recipes, Rubik Pi notes, calibration steps, and demo
procedures live in docs, configs, scripts, and recorded conversation notes.
This README is intentionally a short project overview.
