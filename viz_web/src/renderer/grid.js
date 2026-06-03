// World grid rendering
import * as THREE from 'three';

export class GridRenderer {
    constructor(scene) {
        this.scene = scene;
        this.gridGroup = new THREE.Group();
        this.gridGroup.name = 'grid';

        this.createGrid();
        this.scene.add(this.gridGroup);
    }

    createGrid() {
        // Main ground grid
        const size = 100;
        const divisions = 50;

        const grid = new THREE.GridHelper(size, divisions, 0x333333, 0x111111);
        grid.material.transparent = true;
        grid.material.opacity = 0.15;
        this.gridGroup.add(grid);

        // Axes helper
        const axes = new THREE.AxesHelper(10);
        axes.material.transparent = true;
        axes.material.opacity = 0.6;
        this.gridGroup.add(axes);

        // Add distance markers
        for (let i = -50; i <= 50; i += 10) {
            if (i === 0) continue;

            // X-axis markers
            const markerX = this.createDistanceMarker(`${i}m`, new THREE.Vector3(i, 0, 0));
            this.gridGroup.add(markerX);

            // Y-axis markers
            const markerY = this.createDistanceMarker(`${i}m`, new THREE.Vector3(0, i, 0));
            this.gridGroup.add(markerY);
        }
    }

    createDistanceMarker(text, position) {
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
        sprite.scale.set(2, 0.5, 1);

        return sprite;
    }

    setVisible(visible) {
        this.gridGroup.visible = visible;
    }

    updateScale(scaleValue) {
        // scaleValue: 0-100
        // Adjust grid size based on scale
        const size = 10 + (scaleValue / 100) * 10000;
        // Could dynamically recreate grid here
    }
}
