from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from skyweave.calibration.charuco_live_state import LiveState, _is_running


def _make_handler(state: LiveState):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path in {"/", "/index.html"}:
                _send_bytes(self, _html_page().encode("utf-8"), "text/html; charset=utf-8")
            elif parsed.path == "/status.json":
                _send_json(self, state.snapshot())
            elif parsed.path == "/select":
                query = parse_qs(parsed.query)
                index_text = query.get("index", [""])[0]
                try:
                    index = int(index_text)
                except ValueError:
                    self.send_error(400, "invalid camera index")
                    return
                if not state.request_camera(index):
                    self.send_error(404, "camera index not found")
                    return
                _send_json(self, {"selected": index})
            elif parsed.path == "/snapshot.jpg":
                index = _query_index(state, parsed.query)
                if index is None:
                    self.send_error(404, "camera index not found")
                    return
                jpeg = _wait_for_frame(state, index=index, timeout_s=2.0)
                if jpeg is None:
                    self.send_error(503, "No frame available")
                else:
                    _send_bytes(self, jpeg, "image/jpeg")
            elif parsed.path == "/stream.mjpg":
                index = _query_index(state, parsed.query)
                if index is None:
                    self.send_error(404, "camera index not found")
                    return
                self._stream(index)
            else:
                self.send_error(404)

        def log_message(self, format: str, *args) -> None:
            return None

        def _stream(self, index: int) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            last_version = -1
            while _is_running(state):
                with state.condition:
                    state.condition.wait_for(
                        lambda: (
                            state.frame_jpegs[index] is not None
                            and state.frame_versions[index] != last_version
                        )
                        or not state.running,
                        timeout=2.0,
                    )
                    if state.frame_jpegs[index] is None:
                        continue
                    frame = state.frame_jpegs[index]
                    last_version = state.frame_versions[index]
                try:
                    self.wfile.write(b"--frame\r\n")
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(f"Content-Length: {len(frame)}\r\n\r\n".encode("ascii"))
                    self.wfile.write(frame)
                    self.wfile.write(b"\r\n")
                except (BrokenPipeError, ConnectionResetError):
                    break

    return Handler

def _html_page() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Skyweave ChArUco Live</title>
  <style>
    body { margin: 0; background: #0b0b0b; color: #e8e8e8; font-family: system-ui, sans-serif; }
    header { display: flex; gap: 24px; align-items: center; padding: 12px 16px; background: #151515; border-bottom: 1px solid #333; flex-wrap: wrap; }
    h1 { font-size: 16px; margin: 0; letter-spacing: 0.08em; }
    button { background: #202020; color: #eee; border: 1px solid #555; border-radius: 4px; padding: 7px 10px; cursor: pointer; }
    button.selected { border-color: #7ee787; color: #7ee787; }
    button.failed { border-color: #ff7b72; color: #ff7b72; }
    main { display: grid; grid-template-columns: minmax(0, 1fr) 340px; min-height: calc(100vh - 49px); }
    #streams { display: grid; grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); gap: 10px; padding: 10px; align-content: start; }
    .card { border: 1px solid #333; background: #101010; min-width: 0; }
    .card.selected { border-color: #7ee787; }
    .card.failed { border-color: #ff7b72; }
    .card img { width: 100%; aspect-ratio: 16 / 10; object-fit: contain; background: #050505; display: block; }
    .cardHeader { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 8px 10px; border-bottom: 1px solid #282828; }
    .cardTitle { font-weight: 700; font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .cardStats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; padding: 8px 10px; font-variant-numeric: tabular-nums; font-size: 12px; color: #bbb; }
    .cardStats strong { display: block; color: #eee; font-size: 15px; }
    aside { border-left: 1px solid #333; padding: 16px; background: #101010; }
    .row { display: flex; justify-content: space-between; gap: 16px; border-bottom: 1px solid #262626; padding: 8px 0; }
    .label { color: #999; }
    .value { font-variant-numeric: tabular-nums; text-align: right; }
    .good { color: #7ee787; }
    .bad { color: #ff7b72; }
    #cameraButtons { display: flex; gap: 8px; flex-wrap: wrap; }
  </style>
</head>
<body>
  <header>
    <h1>SKYWEAVE CHARUCO LIVE</h1>
    <span id="status">waiting</span>
    <div id="cameraButtons"></div>
  </header>
  <main>
    <section id="streams"></section>
    <aside id="stats"></aside>
  </main>
  <script>
    async function selectCamera(index) {
      await fetch('/select?index=' + encodeURIComponent(index), {cache: 'no-store'});
      await refresh();
    }
    function cameraButton(camera) {
      const label = 'Cam ' + camera.index + ' ' + camera.device;
      const classes = [camera.selected ? 'selected' : '', camera.status === 'failed' ? 'failed' : ''].join(' ');
      return `<button class="${classes}" onclick="selectCamera(${camera.index})">${label}</button>`;
    }
    function ensureCards(cameras) {
      const root = document.getElementById('streams');
      const key = cameras.map(camera => camera.index + ':' + camera.device).join('|');
      if (root.dataset.key === key) return;
      root.dataset.key = key;
      root.innerHTML = cameras.map(camera => `
        <article class="card" id="card-${camera.index}" onclick="selectCamera(${camera.index})">
          <div class="cardHeader">
            <span class="cardTitle">Cam ${camera.index} · ${camera.device}</span>
            <span id="cam-status-${camera.index}">opening</span>
          </div>
          <img src="/stream.mjpg?index=${camera.index}" alt="Camera ${camera.index} stream">
          <div class="cardStats">
            <span>Corners<strong id="cam-corners-${camera.index}">--</strong></span>
            <span>Markers<strong id="cam-markers-${camera.index}">--</strong></span>
            <span>FPS<strong id="cam-fps-${camera.index}">--</strong></span>
          </div>
        </article>
      `).join('');
    }
    function updateCards(cameras) {
      ensureCards(cameras);
      for (const camera of cameras) {
        const card = document.getElementById('card-' + camera.index);
        if (!card) continue;
        card.className = ['card', camera.selected ? 'selected' : '', camera.status === 'failed' ? 'failed' : ''].join(' ');
        document.getElementById('cam-status-' + camera.index).textContent = camera.status;
        document.getElementById('cam-status-' + camera.index).className = camera.corner_count >= 24 ? 'good' : (camera.status === 'failed' ? 'bad' : '');
        document.getElementById('cam-corners-' + camera.index).textContent = camera.corner_count;
        document.getElementById('cam-corners-' + camera.index).className = camera.corner_count >= 24 ? 'good' : 'bad';
        document.getElementById('cam-markers-' + camera.index).textContent = camera.marker_count;
        document.getElementById('cam-fps-' + camera.index).textContent = camera.capture_fps.toFixed(1);
      }
    }
    async function refresh() {
      const r = await fetch('/status.json', {cache: 'no-store'});
      const s = await r.json();
      const ok = s.corner_count >= 12;
      const stale = s.stale_age_ms > 1500;
      document.getElementById('status').textContent = stale ? 'STREAM STALE' : (ok ? 'DETECTED' : (s.status === 'failed' ? 'CAMERA FAILED' : 'SEARCHING'));
      document.getElementById('status').className = ok && !stale ? 'good' : 'bad';
      document.getElementById('cameraButtons').innerHTML = s.cameras.map(cameraButton).join('');
      updateCards(s.cameras);
      const rows = [
        ['Selected', 'Cam ' + s.selected_index],
        ['Device', s.device],
        ['Status', s.status],
        ['Corners', s.corner_count],
        ['Markers', s.marker_count],
        ['Dictionary', s.dictionary],
        ['Best Corners', s.best_corner_count],
        ['Best Dict', s.best_dictionary],
        ['Sharpness', s.sharpness.toFixed(1)],
        ['Best Sharpness', s.best_sharpness.toFixed(1)],
        ['Detect Rate', (s.detection_rate * 100).toFixed(1) + '%'],
        ['FPS', s.capture_fps.toFixed(1)],
        ['Latency', s.latency_ms.toFixed(2) + ' ms'],
        ['Frame Age', s.stale_age_ms.toFixed(0) + ' ms'],
        ['Frame', s.frame_seq],
        ['Read Failures', s.read_failures],
        ['Error', s.error || 'none']
      ];
      document.getElementById('stats').innerHTML = rows.map(([k, v]) =>
        `<div class="row"><span class="label">${k}</span><span class="value">${v}</span></div>`
      ).join('');
    }
    setInterval(refresh, 250);
    refresh();
  </script>
</body>
</html>
"""

def _send_json(handler: BaseHTTPRequestHandler, payload: dict[str, object]) -> None:
    _send_bytes(handler, json.dumps(payload, sort_keys=True).encode("utf-8"), "application/json")

def _send_bytes(handler: BaseHTTPRequestHandler, payload: bytes, content_type: str) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(payload)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(payload)

def _query_index(state: LiveState, query: str) -> int | None:
    parsed = parse_qs(query)
    if "index" not in parsed:
        with state.lock:
            return state.selected_index
    try:
        index = int(parsed["index"][0])
    except (ValueError, IndexError):
        return None
    return index if 0 <= index < len(state.cameras) else None

def _wait_for_frame(state: LiveState, index: int | None = None, timeout_s: float = 2.0) -> bytes | None:
    with state.condition:
        camera_index = state.selected_index if index is None else index
        state.condition.wait_for(
            lambda: state.frame_jpegs[camera_index] is not None or not state.running,
            timeout=timeout_s,
        )
        return state.frame_jpegs[camera_index]

def _display_host(host: str) -> str:
    return "10.42.0.111" if host in {"0.0.0.0", "::"} else host
