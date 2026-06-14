export class WSClient {
    constructor(state, url = null) {
        this.state = state;
        this.url = url || websocketUrl();
        this.socket = null;
        this.reconnectDelayMs = 1000;
        this.closedByUser = false;
        this.onMessage = null;
        this.onConnectionChange = null;
    }

    async connect() {
        this.closedByUser = false;
        this.open();
    }

    open() {
        this.socket = new WebSocket(this.url);
        this.socket.addEventListener('open', () => {
            this.reconnectDelayMs = 1000;
            this.onConnectionChange?.(true);
        });
        this.socket.addEventListener('message', event => {
            const payload = JSON.parse(event.data);
            this.onMessage?.(payload);
        });
        this.socket.addEventListener('close', () => {
            this.onConnectionChange?.(false);
            if (!this.closedByUser) {
                window.setTimeout(() => this.open(), this.reconnectDelayMs);
                this.reconnectDelayMs = Math.min(this.reconnectDelayMs * 1.5, 5000);
            }
        });
        this.socket.addEventListener('error', () => {
            this.socket?.close();
        });
    }

    close() {
        this.closedByUser = true;
        this.socket?.close();
    }
}

function websocketUrl() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${protocol}//${window.location.host}/ws`;
}
