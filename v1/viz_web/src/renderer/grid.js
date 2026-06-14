// World grid rendering
import * as THREE from 'three';

const GRID_MIN_SIZE_M = 100;
const GRID_MAX_SIZE_M = 20000;
const ROOM_GRID_MIN_SIZE_M = 3;
const ROOM_GRID_MAX_SIZE_M = 16;
const AIRSPACE_GRID_MIN_SIZE_M = 24;
const AIRSPACE_GRID_MAX_SIZE_M = 180;
const GRID_DIVISIONS = 50;
const MARKER_COUNT_PER_AXIS = 5;
const LABEL_SCALE = [40, 10, 1];
const ROOM_LABEL_SCALE = [0.35, 0.09, 1];
const AIRSPACE_LABEL_SCALE = [1.8, 0.45, 1];

export class GridRenderer {
    constructor(scene) {
        this.scene = scene;
        this.gridGroup = new THREE.Group();
        this.gridGroup.name = 'grid';

        this.createGrid(GRID_MIN_SIZE_M, 'map');
        this.scene.add(this.gridGroup);
    }

    createGrid(size, sceneMode = 'map') {
        const isRoom = sceneMode === 'room';
        // Main ground grid
        const grid = new THREE.GridHelper(size, GRID_DIVISIONS, 0x6fb7ff, 0x2f3f52);
        grid.material.transparent = true;
        grid.material.opacity = isRoom ? 0.18 : 0.34;
        this.gridGroup.add(grid);

        // Axes helper
        const axes = new THREE.AxesHelper(size / 10);
        axes.material.transparent = true;
        axes.material.opacity = 0.9;
        this.gridGroup.add(axes);

        // Add distance markers
        const halfSize = size / 2;
        const step = this.niceStep(halfSize / MARKER_COUNT_PER_AXIS);
        for (let i = -halfSize; i <= halfSize; i += step) {
            if (i === 0) continue;
            const label = `${Math.round(i)}m`;

            // X-axis markers
            const markerX = this.createDistanceMarker(label, new THREE.Vector3(i, 0, 0), sceneMode);
            this.gridGroup.add(markerX);

            // Y-axis markers
            const markerY = this.createDistanceMarker(label, new THREE.Vector3(0, i, 0), sceneMode);
            this.gridGroup.add(markerY);
        }
    }

    createDistanceMarker(text, position, sceneMode = 'map') {
        const canvas = document.createElement('canvas');
        const context = canvas.getContext('2d');
        canvas.width = 128;
        canvas.height = 32;

        context.fillStyle = 'rgba(0, 0, 0, 0)';
        context.fillRect(0, 0, canvas.width, canvas.height);

        context.font = '18px Inter, sans-serif';
        context.fillStyle = 'rgba(255, 255, 255, 0.4)';
        context.textAlign = 'center';
        context.textBaseline = 'middle';
        context.fillText(text, canvas.width / 2, canvas.height / 2);

        const texture = new THREE.CanvasTexture(canvas);
        const material = new THREE.SpriteMaterial({
            map: texture,
            transparent: true
        });

        const sprite = new THREE.Sprite(material);
        sprite.position.copy(position);
        const scale = sceneMode === 'room' ? ROOM_LABEL_SCALE : sceneMode === 'airspace' ? AIRSPACE_LABEL_SCALE : LABEL_SCALE;
        sprite.scale.set(...scale);

        return sprite;
    }

    setVisible(visible) {
        this.gridGroup.visible = visible;
    }

    updateScale(scaleValue, sceneMode = 'map') {
        const t = Math.min(100, Math.max(0, scaleValue)) / 100;
        const minSize = sceneMode === 'room'
            ? ROOM_GRID_MIN_SIZE_M
            : sceneMode === 'airspace'
                ? AIRSPACE_GRID_MIN_SIZE_M
                : GRID_MIN_SIZE_M;
        const maxSize = sceneMode === 'room'
            ? ROOM_GRID_MAX_SIZE_M
            : sceneMode === 'airspace'
                ? AIRSPACE_GRID_MAX_SIZE_M
                : GRID_MAX_SIZE_M;
        const size = minSize * Math.pow(maxSize / minSize, t);

        this.clearGrid();
        this.createGrid(size, sceneMode);
    }

    clearGrid() {
        while (this.gridGroup.children.length > 0) {
            const child = this.gridGroup.children.pop();
            if (child.geometry) child.geometry.dispose();
            this.disposeMaterial(child.material);
        }
    }

    disposeMaterial(material) {
        if (!material) {
            return;
        }
        if (Array.isArray(material)) {
            material.forEach(item => this.disposeMaterial(item));
            return;
        }
        if (material.map) material.map.dispose();
        material.dispose();
    }

    niceStep(value) {
        const exponent = Math.floor(Math.log10(value));
        const fraction = value / Math.pow(10, exponent);
        const niceFraction = fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 5 ? 5 : 10;
        return niceFraction * Math.pow(10, exponent);
    }
}
