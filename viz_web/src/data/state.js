export class VizState {
    constructor() {
        const params = new URLSearchParams(window.location.search);
        const initialSceneMode = params.get('view') === 'room' ? 'room' : 'map';
        this.tracks = new Map();
        this.cameras = new Map();
        this.measurements = [];
        this.weavefieldHistory = [];
        this.stats = {};
        this.room = {
            mesh_url: '',
            visible: true,
            opacity: 0.42,
            scale: 1.0,
            translation_m: [0, 0, 0],
            rotation_deg: [0, 0, 0],
            fallback_visible: true,
            fallback_size_m: [4, 4, 2.6],
            revision: 0
        };
        this.selectedTrackId = null;
        this.followingTrackId = null;
        this.visibility = {
            cameras: true,
            frustums: initialSceneMode !== 'room',
            voxels: initialSceneMode !== 'room',
            tracks: true,
            trails: true,
            grid: true,
            rays: 'none'
        };
        this.settings = {
            autoZoom: true,
            scaleValue: initialSceneMode === 'room' ? 8 : 50,
            sceneMode: initialSceneMode,
            glyphScale: initialSceneMode === 'room' ? 0.0015 : 1.0,
            frustumRangeM: initialSceneMode === 'room' ? 1.0 : 650.0
        };
        this.subscribers = new Set();
    }

    subscribe(callback) {
        this.subscribers.add(callback);
        return () => this.subscribers.delete(callback);
    }

    notify(event) {
        this.subscribers.forEach(callback => callback(event, this));
    }

    updateTracks(tracks) {
        this.tracks = new Map((tracks || []).map(track => [track.id, track]));
        if (this.selectedTrackId !== null && !this.tracks.has(this.selectedTrackId)) {
            this.selectedTrackId = null;
        }
        if (this.followingTrackId !== null && !this.tracks.has(this.followingTrackId)) {
            this.followingTrackId = null;
        }
        this.notify('tracks');
    }

    updateCameras(cameras) {
        this.cameras = new Map((cameras || []).map(camera => [camera.id, camera]));
        this.notify('cameras');
    }

    updateWeavefield(weavefieldHistory) {
        this.weavefieldHistory = weavefieldHistory || [];
        this.notify('weavefield');
    }

    updateMeasurements(measurements) {
        this.measurements = measurements || [];
        this.notify('measurements');
    }

    updateStats(stats) {
        this.stats = { ...this.stats, ...(stats || {}) };
        this.notify('stats');
    }

    updateRoom(room) {
        this.room = { ...this.room, ...(room || {}) };
        this.notify('room');
    }

    setVisibility(key, value) {
        this.visibility = { ...this.visibility, [key]: value };
        this.notify('visibility');
    }

    setSetting(key, value) {
        this.settings = { ...this.settings, [key]: value };
        this.notify('settings');
    }

    selectTrack(trackId) {
        this.selectedTrackId = trackId;
        this.notify('selection');
    }

    followTrack(trackId) {
        this.followingTrackId = trackId;
        this.notify('follow');
    }

    getActiveTrack() {
        if (this.selectedTrackId === null) {
            return null;
        }
        return this.tracks.get(this.selectedTrackId) || null;
    }

    getFollowingTrack() {
        if (this.followingTrackId === null) {
            return null;
        }
        return this.tracks.get(this.followingTrackId) || null;
    }
}
