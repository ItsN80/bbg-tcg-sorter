#!/usr/bin/env python3
"""
Camera service (runs as the web app's user via systemd: bbg-camera.service).

Only one process can open the camera at a time, so this service owns it and
keeps it running, which also keeps auto-exposure settled between cards. Other
processes talk to it over HTTP on localhost:

  GET /capture      one full-resolution JPEG (camera_client.STILL_SIZE), from
                    a frame that started after the request arrived
  GET /stream.mjpg  live low-resolution MJPEG view; the web app proxies it to
                    the browser behind its login
  GET /health       {"ok": true, "viewers": n}

The stream is encoded by the Pi's hardware JPEG encoder, which only runs while
someone is watching. If a capture fails the service exits so systemd restarts
it with a freshly opened camera.
"""
import io
import json
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("LIBCAMERA_LOG_LEVELS", "3")  # must be set before picamera2 loads libcamera

from picamera2 import Picamera2
from picamera2.encoders import MJPEGEncoder
from picamera2.outputs import FileOutput

from camera_client import STILL_SIZE

HOST = os.environ.get("BBG_CAMERA_HOST", "127.0.0.1")
PORT = int(os.environ.get("BBG_CAMERA_PORT", "5001"))
# The page shows only the centre the stills keep (Read-Card.py trims 25% of
# the width and 15% of the height off each side), ~640x500 of these pixels.
STREAM_SIZE = (1280, 720)
STREAM_MAX_FPS = 15
# 12-15 fps: the upper limit matches the preview default the stills always used
# (so the same exposure range), the lower one stops the sensor free-running at
# ~25 fps, since every frame costs CPU in this process even with nobody watching.
FRAME_DURATION_LIMITS_US = (1_000_000 // STREAM_MAX_FPS, 83333)
CAPTURE_TIMEOUT_SEC = 5
STREAM_STALL_SEC = 5   # end a stream if the encoder produces nothing for this long
BOUNDARY = "FRAME"


class FrameBroadcast(io.BufferedIOBase):
    """Latest encoded stream frame, shared by every viewer."""

    def __init__(self):
        self.frame = None
        self.seq = 0
        self.cond = threading.Condition()

    def write(self, buf):
        with self.cond:
            self.frame = bytes(buf)  # the encoder reuses its buffer
            self.seq += 1
            self.cond.notify_all()
        return len(buf)


class Camera:
    def __init__(self):
        self.cam = Picamera2()
        # Same main stream as the old per-card capture (preview config, RGB888,
        # STILL_SIZE), so the sensor mode, field of view and crops are unchanged.
        self.cam.configure(self.cam.create_preview_configuration(
            main={"format": "RGB888", "size": STILL_SIZE},
            lores={"size": STREAM_SIZE},
            controls={"FrameDurationLimits": FRAME_DURATION_LIMITS_US}))
        self.cam.start()
        self.broadcast = FrameBroadcast()
        self._capture_lock = threading.Lock()
        self._viewer_lock = threading.Lock()
        self._encoder = None
        self.viewers = 0

    def capture_jpeg(self):
        with self._capture_lock:
            job = self.cam.capture_request(wait=False, flush=True)
            request = self.cam.wait(job, timeout=CAPTURE_TIMEOUT_SEC)
            try:
                buf = io.BytesIO()
                request.save("main", buf, format="jpeg")
            finally:
                request.release()
        return buf.getvalue()

    def add_viewer(self):
        with self._viewer_lock:
            if self.viewers == 0:
                self._encoder = MJPEGEncoder()
                self.cam.start_encoder(self._encoder, FileOutput(self.broadcast), name="lores")
            self.viewers += 1

    def remove_viewer(self):
        with self._viewer_lock:
            self.viewers -= 1
            if self.viewers == 0 and self._encoder is not None:
                self.cam.stop_encoder(self._encoder)
                self._encoder = None

    def close(self):
        try:
            self.cam.stop()
        finally:
            self.cam.close()


camera = None


def _exit_for_restart(reason):
    print(f"Camera failure, exiting so systemd restarts the service: {reason}", file=sys.stderr, flush=True)
    threading.Timer(0.5, os._exit, args=(1,)).start()  # let the error response go out first


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._send_json(200, {"ok": True, "viewers": camera.viewers})
        elif path == "/capture":
            self._capture()
        elif path == "/stream.mjpg":
            self._stream()
        else:
            self._send_json(404, {"ok": False, "error": "not found"})

    def _send_json(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _capture(self):
        try:
            data = camera.capture_jpeg()
        except Exception as e:
            self._send_json(503, {"ok": False, "error": str(e) or type(e).__name__})
            _exit_for_restart(e)
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _stream(self):
        try:
            camera.add_viewer()
        except Exception as e:
            self._send_json(503, {"ok": False, "error": f"could not start the stream encoder: {e}"})
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY}")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            out = camera.broadcast
            last_seq = 0
            while True:
                started = time.monotonic()
                with out.cond:
                    if not out.cond.wait_for(lambda: out.seq != last_seq, timeout=STREAM_STALL_SEC):
                        break
                    frame, last_seq = out.frame, out.seq
                self.wfile.write(f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
                                 f"Content-Length: {len(frame)}\r\n\r\n".encode())
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
                time.sleep(max(0.0, 1.0 / STREAM_MAX_FPS - (time.monotonic() - started)))
        except (BrokenPipeError, ConnectionResetError):
            pass  # viewer went away
        finally:
            camera.remove_viewer()

    def log_message(self, format, *args):
        pass  # one line per frame request would flood the journal


def main():
    global camera
    try:
        camera = Camera()
    except Exception as e:
        print(f"Could not open the camera: {e}", file=sys.stderr, flush=True)
        sys.exit(1)

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    print(f"Camera service listening on http://{HOST}:{PORT}", flush=True)
    try:
        server.serve_forever()
    finally:
        camera.close()


if __name__ == "__main__":
    main()
