"""
Simple visualization server for Skyweave.

Serves the viz_web static files and provides WebSocket streaming of VizFrame data.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from aiohttp import web
from aiohttp import WSMsgType

logger = logging.getLogger(__name__)


class VizServer:
    def __init__(self, viz_dir: Path, host: str = "0.0.0.0", port: int = 8080):
        self.viz_dir = viz_dir
        self.host = host
        self.port = port
        self.app = web.Application()
        self.ws_clients: set[web.WebSocketResponse] = set()

        self._setup_routes()

    def _setup_routes(self):
        self.app.router.add_get("/", self._handle_index)
        self.app.router.add_static("/src", self.viz_dir / "src", name="src")
        self.app.router.add_static("/styles", self.viz_dir / "styles", name="styles")
        assets_dir = self.viz_dir / "assets"
        if assets_dir.exists():
            self.app.router.add_static("/assets", assets_dir, name="assets")
        self.app.router.add_get("/ws", self._handle_websocket)

    async def _handle_index(self, request: web.Request) -> web.Response:
        """Serve the main HTML file."""
        index_path = self.viz_dir / "index.html"
        if not index_path.exists():
            return web.Response(text="index.html not found", status=404)

        return web.FileResponse(index_path)

    async def _handle_websocket(self, request: web.Request) -> web.WebSocketResponse:
        """Handle WebSocket connections for live data streaming."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        self.ws_clients.add(ws)
        logger.info(f"WebSocket client connected. Total clients: {len(self.ws_clients)}")

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    # Handle messages from client if needed
                    data = json.loads(msg.data)
                    logger.debug(f"Received from client: {data}")
                elif msg.type == WSMsgType.ERROR:
                    logger.error(f"WebSocket error: {ws.exception()}")
        finally:
            self.ws_clients.discard(ws)
            logger.info(f"WebSocket client disconnected. Total clients: {len(self.ws_clients)}")

        return ws

    async def broadcast_viz_frame(self, viz_frame: dict[str, Any]) -> None:
        """Broadcast a VizFrame to all connected WebSocket clients."""
        if not self.ws_clients:
            return

        message = json.dumps(viz_frame)

        # Send to all connected clients
        disconnected = set()
        for ws in self.ws_clients:
            try:
                await ws.send_str(message)
            except Exception as exc:
                logger.error("Failed to send to client: %s", exc)
                disconnected.add(ws)

        # Remove disconnected clients
        self.ws_clients -= disconnected

    async def start(self) -> None:
        """Start the visualization server."""
        runner = web.AppRunner(self.app)
        await runner.setup()

        site = web.TCPSite(runner, self.host, self.port)
        await site.start()

        logger.info(f"Visualization server started at http://{self.host}:{self.port}")

    def run(self) -> None:
        """Run the server (blocking)."""
        asyncio.run(self._run_forever())

    async def _run_forever(self) -> None:
        """Keep the server running."""
        await self.start()
        # Keep running until interrupted
        try:
            await asyncio.Event().wait()
        except KeyboardInterrupt:
            logger.info("Server shutting down...")


def build_viz_frame(
    tracks: list[dict],
    cameras: list[dict],
    weavefield_history: list[dict],
    measurements: list[dict] | None = None,
    stats: dict | None = None,
    ts_ns: int = 0,
) -> dict[str, Any]:
    """
    Build a VizFrame dictionary from tracking data.

    This is a helper function to construct the VizFrame format expected by the frontend.
    """
    return {
        "ts_ns": ts_ns,
        "tracks": tracks,
        "cameras": cameras,
        "measurements": measurements or [],
        "weavefield_history": weavefield_history,
        "stats": stats or {},
    }


if __name__ == "__main__":
    # Simple test server
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    viz_dir = Path(__file__).resolve().parents[3] / "viz_web"
    if not viz_dir.exists():
        print(f"Error: viz_web directory not found at {viz_dir}")
        sys.exit(1)

    server = VizServer(viz_dir)
    print("Starting visualization server...")
    print("Open http://localhost:8080 in your browser")

    server.run()
