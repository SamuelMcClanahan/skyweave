// Track sphere and trail rendering
import * as THREE from 'three';

const TRACK_RADIUS_M = 18;
const TRACK_LABEL_OFFSET_M = 35;
const TRACK_LABEL_SCALE = [90, 22, 1];
const MIN_ARROW_LENGTH_M = 45;
const ARROW_SPEED_SCALE = 4;
const ARROW_HEAD_LENGTH_M = 12;
const ARROW_HEAD_WIDTH_M = 8;
const MIN_DIRECTION_SPEED_MPS = 0.01;
const SELECTED_TRACK_COLOR = 0x00e5ff;
const SELECTED_HALO_RADIUS_M = TRACK_RADIUS_M * 1.8;

export class TrackRenderer {
    constructor(scene) {
        this.scene = scene;
        this.trackGroup = new THREE.Group();
        this.trackGroup.name = 'tracks';
        this.scene.add(this.trackGroup);

        this.trackMeshes = new Map();
        this.trailMeshes = new Map();
        this.velocityArrows = new Map();
        this.labels = new Map();
        this.selectionHalos = new Map();
    }

    update(tracks, visibility, selectedTrackId) {
        // Clear old tracks that no longer exist
        const currentIds = new Set(tracks.keys());
        this.trackMeshes.forEach((mesh, trackId) => {
            if (!currentIds.has(trackId)) {
                this.removeTrack(trackId);
            }
        });

        // Update or create tracks
        tracks.forEach(track => {
            this.updateTrack(track, visibility, selectedTrackId);
        });

        this.trackGroup.visible = visibility.tracks;
    }

    updateTrack(track, visibility, selectedTrackId) {
        const position = new THREE.Vector3(track.state[0], track.state[1], track.state[2]);
        const velocity = new THREE.Vector3(track.state[3], track.state[4], track.state[5]);

        // Create or update track sphere
        if (!this.trackMeshes.has(track.id)) {
            this.createTrackMesh(track);
        }

        const trackMesh = this.trackMeshes.get(track.id);
        trackMesh.position.copy(position);

        // Update appearance based on selection
        const isSelected = track.id === selectedTrackId;
        trackMesh.scale.setScalar(isSelected ? 1.5 : 1.0);

        // Update color based on classification
        const color = isSelected ? SELECTED_TRACK_COLOR : this.getTrackColor(track);
        trackMesh.material.color.setHex(color);
        trackMesh.material.emissive.setHex(color);
        trackMesh.material.emissiveIntensity = isSelected ? 0.9 : 0.3;

        if (this.selectionHalos.has(track.id)) {
            const halo = this.selectionHalos.get(track.id);
            halo.position.copy(position);
            halo.visible = isSelected;
        }

        // Update trail
        if (visibility.trails && track.trail && track.trail.length > 1) {
            this.updateTrail(track);
        } else if (this.trailMeshes.has(track.id)) {
            this.trackGroup.remove(this.trailMeshes.get(track.id));
            this.trailMeshes.delete(track.id);
        }

        // Update velocity arrow
        if (this.velocityArrows.has(track.id)) {
            const arrow = this.velocityArrows.get(track.id);
            arrow.position.copy(position);
            const speed = Math.sqrt(track.state[3]**2 + track.state[4]**2 + track.state[5]**2);
            if (speed > MIN_DIRECTION_SPEED_MPS) {
                arrow.setDirection(velocity.clone().normalize());
            }
            arrow.setLength(
                Math.max(MIN_ARROW_LENGTH_M, speed * ARROW_SPEED_SCALE),
                ARROW_HEAD_LENGTH_M,
                ARROW_HEAD_WIDTH_M
            );
        }

        // Update label
        if (this.labels.has(track.id)) {
            const label = this.labels.get(track.id);
            label.position.copy(position);
            label.position.z += TRACK_LABEL_OFFSET_M;
        }
    }

    createTrackMesh(track) {
        const group = new THREE.Group();

        // Main track sphere
        const geometry = new THREE.SphereGeometry(TRACK_RADIUS_M, 24, 24);
        const material = new THREE.MeshStandardMaterial({
            color: this.getTrackColor(track),
            emissive: this.getTrackColor(track),
            emissiveIntensity: 0.3,
            metalness: 0.1,
            roughness: 0.6
        });

        const mesh = new THREE.Mesh(geometry, material);
        mesh.userData.trackId = track.id;
        group.add(mesh);

        const haloGeometry = new THREE.SphereGeometry(SELECTED_HALO_RADIUS_M, 24, 24);
        const haloMaterial = new THREE.MeshBasicMaterial({
            color: SELECTED_TRACK_COLOR,
            transparent: true,
            opacity: 0.22,
            wireframe: true,
            depthWrite: false
        });
        const halo = new THREE.Mesh(haloGeometry, haloMaterial);
        halo.visible = false;
        group.add(halo);
        this.selectionHalos.set(track.id, halo);

        // Velocity arrow
        const arrowColor = this.getTrackColor(track);
        const arrow = new THREE.ArrowHelper(
            new THREE.Vector3(1, 0, 0),
            new THREE.Vector3(0, 0, 0),
            MIN_ARROW_LENGTH_M,
            arrowColor,
            ARROW_HEAD_LENGTH_M,
            ARROW_HEAD_WIDTH_M
        );
        group.add(arrow);
        this.velocityArrows.set(track.id, arrow);

        // Label
        const label = this.createLabel(`#${track.id}`);
        group.add(label);
        this.labels.set(track.id, label);

        this.trackMeshes.set(track.id, mesh);
        this.trackGroup.add(group);
    }

    updateTrail(track) {
        // Remove old trail if it exists
        if (this.trailMeshes.has(track.id)) {
            const oldTrail = this.trailMeshes.get(track.id);
            this.trackGroup.remove(oldTrail);
            oldTrail.geometry.dispose();
            oldTrail.material.dispose();
        }

        // Create trail line
        const points = track.trail.map(p => new THREE.Vector3(p[0], p[1], p[2]));

        if (points.length < 2) return;

        const geometry = new THREE.BufferGeometry().setFromPoints(points);

        // Create gradient colors for trail (fade from bright to dim)
        const colors = [];
        const color = new THREE.Color(this.getTrackColor(track));
        for (let i = 0; i < points.length; i++) {
            const alpha = i / (points.length - 1);
            colors.push(color.r * alpha, color.g * alpha, color.b * alpha);
        }
        geometry.setAttribute('color', new THREE.Float32BufferAttribute(colors, 3));

        const material = new THREE.LineBasicMaterial({
            vertexColors: true,
            linewidth: 2,
            transparent: true,
            opacity: 0.6
        });

        const trail = new THREE.Line(geometry, material);
        this.trailMeshes.set(track.id, trail);
        this.trackGroup.add(trail);
    }

    removeTrack(trackId) {
        if (this.trackMeshes.has(trackId)) {
            const mesh = this.trackMeshes.get(trackId);
            const group = mesh.parent;
            this.trackGroup.remove(group);

            mesh.geometry.dispose();
            mesh.material.dispose();
            this.trackMeshes.delete(trackId);
        }

        if (this.selectionHalos.has(trackId)) {
            const halo = this.selectionHalos.get(trackId);
            halo.geometry.dispose();
            halo.material.dispose();
            this.selectionHalos.delete(trackId);
        }

        if (this.trailMeshes.has(trackId)) {
            const trail = this.trailMeshes.get(trackId);
            this.trackGroup.remove(trail);
            trail.geometry.dispose();
            trail.material.dispose();
            this.trailMeshes.delete(trackId);
        }

        this.velocityArrows.delete(trackId);
        this.labels.delete(trackId);
    }

    getTrackColor(track) {
        const statusColors = {
            'candidate': 0xcccccc,
            'active': 0xffffff,
            'coasting': 0x666666
        };

        const classificationColors = {
            'drone': 0xffffff,
            'plane': 0xffffff,
            'bird': 0xffb800,  // Yellow accent for birds
            'unknown': 0xcccccc
        };

        // Prefer classification color if available
        if (track.classification && classificationColors[track.classification]) {
            return classificationColors[track.classification];
        }

        return statusColors[track.status] || 0xffffff;
    }

    createLabel(text) {
        const canvas = document.createElement('canvas');
        const context = canvas.getContext('2d');
        canvas.width = 256;
        canvas.height = 64;

        context.fillStyle = 'rgba(0, 0, 0, 0)';
        context.fillRect(0, 0, canvas.width, canvas.height);

        context.font = '32px Inter, sans-serif';
        context.fillStyle = '#ffffff';
        context.textAlign = 'center';
        context.textBaseline = 'middle';
        context.fillText(text, canvas.width / 2, canvas.height / 2);

        const texture = new THREE.CanvasTexture(canvas);
        const material = new THREE.SpriteMaterial({
            map: texture,
            transparent: true
        });

        const sprite = new THREE.Sprite(material);
        sprite.scale.set(...TRACK_LABEL_SCALE);

        return sprite;
    }

    raycast(raycaster) {
        const intersects = raycaster.intersectObjects(Array.from(this.trackMeshes.values()));
        if (intersects.length > 0) {
            return intersects[0].object.userData.trackId;
        }
        return null;
    }
}
