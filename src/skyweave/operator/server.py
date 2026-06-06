from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType
from aiohttp import web

from skyweave.calibration.charuco_live_server import _display_host
from skyweave.calibration.charuco_live_state import _is_running
from skyweave.operator.profiles import list_profiles, load_profile, save_profile
from skyweave.operator.state import OperatorState


class OperatorServer:
    def __init__(
        self,
        state: OperatorState,
        viz_dir: Path,
        room_assets_dir: Path,
        host: str = "0.0.0.0",
        port: int = 8088,
    ) -> None:
        self.state = state
        self.viz_dir = viz_dir
        self.room_assets_dir = room_assets_dir
        self.host = host
        self.port = port
        self.app = web.Application()
        self.ws_clients: set[web.WebSocketResponse] = set()
        self._broadcast_task: asyncio.Task | None = None
        self._setup_routes()

    def _setup_routes(self) -> None:
        self.app.on_startup.append(self._on_startup)
        self.app.on_cleanup.append(self._on_cleanup)
        self.app.router.add_get("/", self._redirect_operator)
        self.app.router.add_get("/operator", self._handle_operator)
        self.app.router.add_get("/api/status", self._handle_status)
        self.app.router.add_get("/api/room", self._handle_room)
        self.app.router.add_route("PATCH", "/api/settings", self._handle_settings)
        self.app.router.add_route("POST", "/api/settings", self._handle_settings)
        self.app.router.add_get("/api/record", self._handle_record_status)
        self.app.router.add_post("/api/record/start", self._handle_record_start)
        self.app.router.add_post("/api/record/stop", self._handle_record_stop)
        self.app.router.add_post("/api/record/snapshot", self._handle_record_snapshot)
        self.app.router.add_get("/api/profiles", self._handle_profiles)
        self.app.router.add_put("/api/profiles/{name}", self._handle_profile_save)
        self.app.router.add_post("/api/profiles/{name}/load", self._handle_profile_load)
        self.app.router.add_get("/select", self._handle_select)
        self.app.router.add_post("/select", self._handle_select)
        self.app.router.add_get("/snapshot.jpg", self._handle_snapshot)
        self.app.router.add_get("/stream.mjpg", self._handle_stream)
        self.app.router.add_get("/ws", self._handle_websocket)
        self.app.router.add_get("/viz", self._redirect_viz)
        self.app.router.add_get("/viz/", self._handle_viz_index)
        self.app.router.add_static("/viz/src", self.viz_dir / "src", name="viz_src")
        self.app.router.add_static("/viz/styles", self.viz_dir / "styles", name="viz_styles")
        assets_dir = self.viz_dir / "assets"
        if assets_dir.exists():
            self.app.router.add_static("/viz/assets", assets_dir, name="viz_assets")
        self.room_assets_dir.mkdir(parents=True, exist_ok=True)
        self.app.router.add_static("/room-assets", self.room_assets_dir, name="room_assets")

    async def _on_startup(self, _app: web.Application) -> None:
        self._broadcast_task = asyncio.create_task(self._broadcast_loop())

    async def _on_cleanup(self, _app: web.Application) -> None:
        if self._broadcast_task is not None:
            self._broadcast_task.cancel()
            try:
                await self._broadcast_task
            except asyncio.CancelledError:
                pass
        for ws in set(self.ws_clients):
            await ws.close()

    async def _redirect_operator(self, _request: web.Request) -> web.Response:
        raise web.HTTPFound("/operator")

    async def _redirect_viz(self, _request: web.Request) -> web.Response:
        raise web.HTTPFound("/viz/")

    async def _handle_operator(self, _request: web.Request) -> web.Response:
        return web.Response(text=_operator_html(), content_type="text/html")

    async def _handle_viz_index(self, _request: web.Request) -> web.StreamResponse:
        index_path = self.viz_dir / "index.html"
        if not index_path.exists():
            raise web.HTTPNotFound(text="viz_web/index.html not found")
        return web.FileResponse(index_path)

    async def _handle_status(self, _request: web.Request) -> web.Response:
        return web.json_response(self.state.snapshot())

    async def _handle_room(self, _request: web.Request) -> web.Response:
        return web.json_response(self.state.room.to_dict())

    async def _handle_settings(self, request: web.Request) -> web.Response:
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("settings payload must be an object")
            snapshot = self.state.apply_payload(payload)
        except Exception as exc:
            return _json_error(str(exc))
        return web.json_response(snapshot)

    async def _handle_record_status(self, _request: web.Request) -> web.Response:
        return web.json_response(self.state.snapshot()["recording"])

    async def _handle_record_start(self, request: web.Request) -> web.Response:
        try:
            payload = await _optional_json(request)
            return web.json_response(self.state.start_recording(payload.get("name")))
        except Exception as exc:
            return _json_error(str(exc))

    async def _handle_record_stop(self, _request: web.Request) -> web.Response:
        try:
            return web.json_response(self.state.stop_recording())
        except Exception as exc:
            return _json_error(str(exc))

    async def _handle_record_snapshot(self, request: web.Request) -> web.Response:
        try:
            payload = await _optional_json(request)
            return web.json_response(self.state.save_recording_snapshot(payload.get("name")))
        except Exception as exc:
            return _json_error(str(exc))

    async def _handle_profiles(self, _request: web.Request) -> web.Response:
        return web.json_response({"profiles": list_profiles(self.state.profile_dir)})

    async def _handle_profile_save(self, request: web.Request) -> web.Response:
        try:
            payload = save_profile(self.state, request.match_info["name"])
        except Exception as exc:
            return _json_error(str(exc))
        return web.json_response(payload)

    async def _handle_profile_load(self, request: web.Request) -> web.Response:
        try:
            payload = load_profile(self.state, request.match_info["name"])
        except FileNotFoundError as exc:
            return _json_error(str(exc), status=404)
        except Exception as exc:
            return _json_error(str(exc))
        return web.json_response(payload)

    async def _handle_select(self, request: web.Request) -> web.Response:
        if request.method == "POST":
            payload = await request.json()
            index_text = str(payload.get("index", ""))
        else:
            index_text = request.query.get("index", "")
        try:
            index = int(index_text)
        except ValueError:
            return _json_error("invalid camera index")
        if not self.state.live.request_camera(index):
            return _json_error("camera index not found", status=404)
        return web.json_response({"selected": index})

    async def _handle_snapshot(self, request: web.Request) -> web.Response:
        index = _camera_index_from_request(request, self.state.live.selected_index, len(self.state.live.cameras))
        frame = await asyncio.to_thread(_wait_for_frame, self.state, index, -1, 2.0)
        if frame[1] is None:
            raise web.HTTPServiceUnavailable(text="No frame available")
        return web.Response(body=frame[1], content_type="image/jpeg")

    async def _handle_stream(self, request: web.Request) -> web.StreamResponse:
        index = _camera_index_from_request(request, self.state.live.selected_index, len(self.state.live.cameras))
        stream_fps = _stream_fps_from_request(request, default=8.0)
        min_interval_s = 1.0 / stream_fps
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "multipart/x-mixed-replace; boundary=frame",
                "Cache-Control": "no-store",
            },
        )
        await response.prepare(request)
        last_version = -1
        last_sent_s = 0.0
        while _is_running(self.state.live):
            if last_sent_s > 0.0:
                delay_s = min_interval_s - (time.perf_counter() - last_sent_s)
                if delay_s > 0.0:
                    await asyncio.sleep(delay_s)
            last_version, frame = await asyncio.to_thread(_wait_for_frame, self.state, index, last_version, 2.0)
            if frame is None:
                continue
            try:
                await response.write(b"--frame\r\n")
                await response.write(b"Content-Type: image/jpeg\r\n")
                await response.write(f"Content-Length: {len(frame)}\r\n\r\n".encode("ascii"))
                await response.write(frame)
                await response.write(b"\r\n")
                last_sent_s = time.perf_counter()
            except (ConnectionResetError, BrokenPipeError):
                break
        return response

    async def _handle_websocket(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.ws_clients.add(ws)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.ERROR:
                    break
        finally:
            self.ws_clients.discard(ws)
        return ws

    async def _broadcast_loop(self) -> None:
        last_version = 0
        while _is_running(self.state.live):
            version, frame = await asyncio.to_thread(self.state.wait_viz_frame, last_version, 1.0)
            if version == last_version or frame is None:
                continue
            last_version = version
            await self.broadcast_viz_frame(frame)

    async def broadcast_viz_frame(self, frame: dict[str, Any]) -> None:
        if not self.ws_clients:
            return
        message = json.dumps(frame)
        disconnected = set()
        for ws in self.ws_clients:
            try:
                await ws.send_str(message)
            except Exception:
                disconnected.add(ws)
        self.ws_clients -= disconnected

    async def start(self) -> None:
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()
        print(f"operator_url=http://{_display_host(self.host)}:{self.port}/operator")
        print(f"visualizer_url=http://{_display_host(self.host)}:{self.port}/viz/")

    def run(self) -> None:
        try:
            asyncio.run(self._run_forever())
        except KeyboardInterrupt:
            pass

    async def _run_forever(self) -> None:
        await self.start()
        try:
            await asyncio.Event().wait()
        except KeyboardInterrupt:
            pass


def _camera_index_from_request(request: web.Request, default: int, camera_count: int) -> int:
    index_text = request.query.get("index")
    if index_text is None:
        return default
    try:
        index = int(index_text)
    except ValueError:
        raise web.HTTPBadRequest(text="invalid camera index")
    if index < 0 or index >= camera_count:
        raise web.HTTPNotFound(text="camera index not found")
    return index


def _stream_fps_from_request(request: web.Request, default: float) -> float:
    value = request.query.get("fps")
    if value is None:
        return default
    try:
        fps = float(value)
    except ValueError:
        raise web.HTTPBadRequest(text="invalid stream fps")
    return max(1.0, min(fps, 20.0))


def _wait_for_frame(state: OperatorState, camera_index: int, last_version: int, timeout_s: float) -> tuple[int, bytes | None]:
    with state.live.condition:
        if camera_index < 0 or camera_index >= len(state.live.frame_jpegs):
            return 0, None
        state.live.condition.wait_for(
            lambda: (
                state.live.frame_jpegs[camera_index] is not None
                and state.live.frame_versions[camera_index] != last_version
            )
            or not state.live.running,
            timeout=timeout_s,
        )
        return state.live.frame_versions[camera_index], state.live.frame_jpegs[camera_index]


async def _optional_json(request: web.Request) -> dict[str, Any]:
    if request.content_length == 0:
        return {}
    try:
        payload = await request.json()
    except Exception:
        return {}
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ValueError("request payload must be an object")
    return payload


def _json_error(message: str, status: int = 400) -> web.Response:
    return web.json_response({"error": message}, status=status)


def _operator_html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Skyweave Operator</title>
  <style>
    :root { color-scheme: dark; --bg: #080b0f; --panel: #111720; --line: #263241; --text: #e9eef5; --muted: #95a3b5; --good: #65d18c; --bad: #ff7770; --accent: #6fb7ff; }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--bg); color: var(--text); font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    header { height: 44px; display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 0 14px; background: #0d1219; border-bottom: 1px solid var(--line); }
    h1 { margin: 0; font-size: 13px; letter-spacing: .08em; font-weight: 700; }
    main { height: calc(100vh - 44px); display: grid; grid-template-columns: minmax(430px, 34vw) minmax(0, 1fr); }
    #operator { overflow: auto; border-right: 1px solid var(--line); background: #0c1118; }
    #vizFrame { width: 100%; height: 100%; border: 0; display: block; background: #000; }
    .preview { position: sticky; top: 0; z-index: 2; background: #05070a; border-bottom: 1px solid var(--line); }
    .cameraFeeds { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 6px; padding: 8px; }
    .cameraFeed { position: relative; padding: 0; min-height: 0; overflow: hidden; background: #000; border-radius: 6px; }
    .cameraFeed.selected { border-color: var(--good); }
    .cameraFeed.failed { border-color: var(--bad); }
    .cameraFeed img { width: 100%; aspect-ratio: 16 / 9; object-fit: contain; display: block; background: #000; }
    .cameraFeed span { position: absolute; left: 6px; bottom: 6px; max-width: calc(100% - 12px); padding: 2px 5px; border-radius: 4px; background: rgba(0, 0, 0, .72); color: var(--text); font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .previewBar { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 8px 10px; }
    .cameraButtons { display: flex; gap: 6px; flex-wrap: wrap; }
    button, select, input { border: 1px solid var(--line); background: #151c26; color: var(--text); border-radius: 6px; min-height: 30px; }
    button { padding: 5px 9px; cursor: pointer; font-weight: 600; }
    button.selected { color: var(--good); border-color: var(--good); }
    button.failed { color: var(--bad); border-color: var(--bad); }
    select, input { padding: 4px 7px; width: 100%; font: inherit; font-size: 12px; }
    section { padding: 12px; border-bottom: 1px solid var(--line); }
    h2 { margin: 0 0 9px; font-size: 12px; letter-spacing: .08em; color: #cbd5e1; }
    .grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
    .row { display: grid; grid-template-columns: minmax(140px, 1fr) minmax(100px, 1fr); align-items: center; gap: 8px; margin: 6px 0; }
    .label { color: var(--muted); font-size: 12px; }
    .value { font-variant-numeric: tabular-nums; text-align: right; font-size: 12px; }
    .status.good { color: var(--good); }
    .status.bad { color: var(--bad); }
    .full { grid-column: 1 / -1; }
    .profileRow { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; gap: 8px; }
    .metric { padding: 8px; background: var(--panel); border: 1px solid var(--line); border-radius: 6px; min-height: 58px; }
    .metric .label { display: block; margin-bottom: 5px; }
    .metric .value { text-align: left; font-size: 16px; color: var(--accent); }
    @media (max-width: 980px) { main { grid-template-columns: 1fr; grid-template-rows: minmax(640px, 65vh) minmax(480px, 35vh); } #operator { border-right: 0; } }
  </style>
</head>
<body>
  <header>
    <h1>SKYWEAVE OPERATOR</h1>
    <div><span id="mode">--</span> · <span id="runtime" class="status">starting</span></div>
  </header>
  <main>
    <div id="operator">
      <div class="preview">
        <div class="cameraFeeds">
          <button type="button" id="feed0" class="cameraFeed" onclick="selectCamera(0)"><img src="/stream.mjpg?index=0&fps=6" alt="Camera 1 stream"><span id="feedLabel0">cam1</span></button>
          <button type="button" id="feed1" class="cameraFeed" onclick="selectCamera(1)"><img src="/stream.mjpg?index=1&fps=6" alt="Camera 2 stream"><span id="feedLabel1">cam2</span></button>
          <button type="button" id="feed2" class="cameraFeed" onclick="selectCamera(2)"><img src="/stream.mjpg?index=2&fps=6" alt="Camera 3 stream"><span id="feedLabel2">cam3</span></button>
        </div>
        <div class="previewBar">
          <div id="cameraButtons" class="cameraButtons"></div>
          <span id="selectedCamera" class="label">--</span>
        </div>
      </div>
      <section>
        <h2>Tracking</h2>
        <div class="grid">
          <label class="full"><span class="label">Mode</span><select id="trackingMode"></select></label>
          <div class="metric"><span class="label">Track</span><span class="value" id="trackStatus">--</span></div>
          <div class="metric"><span class="label">Speed</span><span class="value" id="trackSpeed">--</span></div>
          <div class="metric"><span class="label">Position</span><span class="value" id="trackPosition">--</span></div>
          <div class="metric"><span class="label">Pipeline</span><span class="value" id="pipeline">--</span></div>
        </div>
      </section>
      <section>
        <h2>Recording</h2>
        <div class="profileRow">
          <input id="recordName" placeholder="throw-test">
          <button id="recordStart" type="button">Start</button>
          <button id="recordStop" type="button">Stop</button>
        </div>
        <div style="margin-top:8px">
          <button id="recordSnapshot" type="button">Save Frame</button>
        </div>
        <div class="row"><span class="label">Status</span><span class="value" id="recordStatus">--</span></div>
        <div class="row"><span class="label">Frames</span><span class="value" id="recordFrames">--</span></div>
        <div class="row"><span class="label">Path</span><span class="value" id="recordPath">--</span></div>
      </section>
      <section>
        <h2>Camera</h2>
        <div class="grid">
          <label><span class="label">Width</span><input data-path="camera.width" type="number" min="1"></label>
          <label><span class="label">Height</span><input data-path="camera.height" type="number" min="1"></label>
          <label><span class="label">FPS</span><input data-path="camera.fps" type="number" min="1" step="1"></label>
          <label><span class="label">FourCC</span><input data-path="camera.fourcc" maxlength="4"></label>
          <label><span class="label">JPEG Quality</span><input data-path="camera.jpeg_quality" type="number" min="1" max="100"></label>
          <label><span class="label">Detect Every</span><input data-path="camera.detect_every" type="number" min="1"></label>
        </div>
      </section>
      <section>
        <h2>Motion</h2>
        <div class="grid">
          <label><span class="label">Backend</span><select data-path="motion.backend"></select></label>
          <label><span class="label">Threshold</span><input data-path="motion.threshold" type="number" min="0" max="255"></label>
          <label><span class="label">Min Area</span><input data-path="motion.min_area_px" type="number" min="1"></label>
          <label><span class="label">Components</span><input data-path="motion.max_components" type="number" min="1"></label>
          <label><span class="label">Patch Side</span><input data-path="motion.max_patch_side_px" type="number" min="1"></label>
          <label><span class="label">Motion Pixels</span><input data-path="motion.max_motion_pixels" type="number" min="1"></label>
        </div>
      </section>
      <section>
        <h2>Kalman</h2>
        <div class="grid">
          <label><span class="label">Accel Sigma</span><input data-path="kalman.sigma_accel_mps2" type="number" min="0" step="0.1"></label>
          <label><span class="label">Position Var</span><input data-path="kalman.initial_position_var" type="number" min="0.001" step="0.1"></label>
          <label><span class="label">Velocity Var</span><input data-path="kalman.initial_velocity_var" type="number" min="0.001" step="0.1"></label>
          <label><span class="label">Measurement Scale</span><input data-path="kalman.measurement_var_scale" type="number" min="0.001" step="0.1"></label>
          <label><span class="label">Coast Seconds</span><input data-path="kalman.coast_seconds" type="number" min="0" step="0.1"></label>
        </div>
      </section>
      <section>
        <h2>Room Mesh</h2>
        <div class="grid">
          <label class="full"><span class="label">Mesh URL</span><input data-room="mesh_url" placeholder="/room-assets/room.glb"></label>
          <label><span class="label">Visible</span><select data-room="visible"><option value="true">true</option><option value="false">false</option></select></label>
          <label><span class="label">Opacity</span><input data-room="opacity" type="number" min="0" max="1" step="0.05"></label>
          <label><span class="label">Scale</span><input data-room="scale" type="number" min="0.001" step="0.01"></label>
          <label><span class="label">Translation</span><input data-room="translation_m"></label>
          <label><span class="label">Rotation</span><input data-room="rotation_deg"></label>
        </div>
      </section>
      <section>
        <h2>Profiles</h2>
        <div class="profileRow">
          <input id="profileName" placeholder="profile-name">
          <button id="saveProfile">Save</button>
          <button id="loadProfile">Load</button>
        </div>
        <div id="profiles" class="label" style="margin-top:8px"></div>
      </section>
      <section id="stats"></section>
    </div>
    <iframe id="vizFrame" src="/viz/" title="Skyweave visualizer"></iframe>
  </main>
  <script>
    const fields = new Map();
    const roomFields = new Map();
    let applying = false;

    function get(obj, path) {
      return path.split('.').reduce((acc, key) => acc && acc[key], obj);
    }
    function setPath(root, path, value) {
      const keys = path.split('.');
      let cursor = root;
      for (let i = 0; i < keys.length - 1; i++) cursor = cursor[keys[i]] ||= {};
      cursor[keys[keys.length - 1]] = value;
    }
    function typedValue(el) {
      if (el.tagName === 'SELECT' && (el.value === 'true' || el.value === 'false')) return el.value === 'true';
      if (el.type === 'number') return Number(el.value);
      if (el.dataset.room && (el.dataset.room.endsWith('_m') || el.dataset.room.endsWith('_deg'))) return el.value.split(',').map(v => Number(v.trim()));
      return el.value;
    }
    async function patch(payload) {
      await fetch('/api/settings', {method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
      await refresh();
    }
    function wireControls() {
      document.querySelectorAll('[data-path]').forEach(el => {
        fields.set(el.dataset.path, el);
        el.addEventListener('change', () => {
          if (applying) return;
          const payload = {};
          setPath(payload, el.dataset.path, typedValue(el));
          patch(payload);
        });
      });
      document.querySelectorAll('[data-room]').forEach(el => {
        roomFields.set(el.dataset.room, el);
        el.addEventListener('change', () => {
          if (applying) return;
          patch({room: {[el.dataset.room]: typedValue(el)}});
        });
      });
      document.getElementById('trackingMode').addEventListener('change', event => patch({tracking: {requested_mode: event.target.value}}));
      document.getElementById('recordStart').addEventListener('click', startRecording);
      document.getElementById('recordStop').addEventListener('click', stopRecording);
      document.getElementById('recordSnapshot').addEventListener('click', saveRecordSnapshot);
      document.getElementById('saveProfile').addEventListener('click', saveProfile);
      document.getElementById('loadProfile').addEventListener('click', loadProfile);
    }
    async function selectCamera(index) {
      await fetch('/select', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({index})});
      await refresh();
    }
    async function saveProfile() {
      const name = document.getElementById('profileName').value || 'default';
      await fetch('/api/profiles/' + encodeURIComponent(name), {method: 'PUT'});
      await refreshProfiles();
    }
    async function loadProfile() {
      const name = document.getElementById('profileName').value || 'default';
      await fetch('/api/profiles/' + encodeURIComponent(name) + '/load', {method: 'POST'});
      await refresh();
      await refreshProfiles();
    }
    async function startRecording() {
      const name = document.getElementById('recordName').value || 'throw-test';
      await fetch('/api/record/start', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name})});
      await refresh();
    }
    async function stopRecording() {
      await fetch('/api/record/stop', {method: 'POST'});
      await refresh();
    }
    async function saveRecordSnapshot() {
      const name = document.getElementById('recordName').value || 'snapshot';
      await fetch('/api/record/snapshot', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name})});
      await refresh();
    }
    async function refreshProfiles() {
      const response = await fetch('/api/profiles', {cache: 'no-store'});
      const data = await response.json();
      document.getElementById('profiles').textContent = data.profiles.map(p => p.name).join(', ') || 'no profiles';
    }
    function cameraButton(camera) {
      const classes = [camera.selected ? 'selected' : '', camera.status === 'failed' ? 'failed' : ''].join(' ');
      return `<button type="button" class="${classes}" onclick="selectCamera(${camera.index})">${camera.label || 'cam'} ${camera.index}</button>`;
    }
    function renderStatus(s) {
      applying = true;
      document.getElementById('runtime').textContent = s.operator.status;
      document.getElementById('runtime').className = 'status ' + (s.operator.error ? 'bad' : 'good');
      document.getElementById('mode').textContent = s.tracking.effective_mode + ' / ' + s.tracking.requested_mode;
      document.getElementById('cameraButtons').innerHTML = s.cameras.map(cameraButton).join('');
      document.getElementById('selectedCamera').textContent = s.device;
      s.cameras.forEach(camera => {
        const feed = document.getElementById('feed' + camera.index);
        const label = document.getElementById('feedLabel' + camera.index);
        if (!feed || !label) return;
        feed.className = 'cameraFeed' + (camera.selected ? ' selected' : '') + (camera.status === 'failed' ? ' failed' : '');
        label.textContent = `${camera.label || 'cam'} · ${camera.capture_fps.toFixed(1)} fps`;
      });
      const modeSelect = document.getElementById('trackingMode');
      modeSelect.innerHTML = s.tracking.mode_choices.map(m => `<option value="${m}">${m}</option>`).join('');
      modeSelect.value = s.tracking.requested_mode;
      for (const [path, el] of fields.entries()) {
        const value = get(s.settings, path);
        if (path === 'motion.backend') {
          el.innerHTML = s.settings.motion.backend_choices.map(v => `<option value="${v}">${v}</option>`).join('');
        }
        if (value !== undefined && document.activeElement !== el) el.value = value;
      }
      for (const [key, el] of roomFields.entries()) {
        const value = s.room[key];
        if (value !== undefined && document.activeElement !== el) el.value = Array.isArray(value) ? value.join(', ') : String(value);
      }
      document.getElementById('trackStatus').textContent = s.track.status + (s.track.track_id ? ` #${s.track.track_id}` : '');
      document.getElementById('trackSpeed').textContent = s.track.speed_mps.toFixed(2) + ' m/s';
      document.getElementById('trackPosition').textContent = s.track.position_m.map(v => v.toFixed(2)).join(', ');
      document.getElementById('pipeline').textContent = `${s.pipeline.packet_count} pkts · ${s.pipeline.blob_count} blobs · ${s.pipeline.measurement_count} meas · ${s.pipeline.track_count} trk`;
      document.getElementById('recordStatus').textContent = s.recording.active ? 'recording' : 'idle';
      document.getElementById('recordFrames').textContent = `${s.recording.frame_count} frames · ${s.recording.image_count} images`;
      document.getElementById('recordPath').textContent = s.recording.output_dir || '--';
      const rows = [
        ['Corners', s.corner_count], ['Markers', s.marker_count], ['Detect Rate', (s.detection_rate * 100).toFixed(1) + '%'],
        ['FPS', s.capture_fps.toFixed(1)], ['Latency', s.latency_ms.toFixed(2) + ' ms'], ['Sharpness', s.sharpness.toFixed(1)],
        ['Stages', `read ${s.pipeline.camera_read_ms.toFixed(1)} · motion ${s.pipeline.motion_ms.toFixed(1)} · preview ${s.pipeline.preview_ms.toFixed(1)} ms`],
        ['Fusion', `align ${s.pipeline.alignment_ms.toFixed(2)} · score ${s.pipeline.scoring_ms.toFixed(2)} · peaks ${s.pipeline.peaks_ms.toFixed(2)} · KF ${s.pipeline.kalman_ms.toFixed(2)} ms`],
        ['Loop Budget', `${s.pipeline.total_ms.toFixed(1)} ms work · ${s.pipeline.target_sleep_ms.toFixed(1)} ms sleep`],
        ['Calibration', s.calibration.message], ['Pipeline Reason', s.pipeline.reason], ['Error', s.error || s.operator.error || 'none']
      ];
      document.getElementById('stats').innerHTML = '<h2>Status</h2>' + rows.map(([k, v]) => `<div class="row"><span class="label">${k}</span><span class="value">${v}</span></div>`).join('');
      applying = false;
    }
    async function refresh() {
      const response = await fetch('/api/status', {cache: 'no-store'});
      renderStatus(await response.json());
    }
    wireControls();
    refresh();
    refreshProfiles();
    setInterval(refresh, 600);
  </script>
</body>
</html>
"""
