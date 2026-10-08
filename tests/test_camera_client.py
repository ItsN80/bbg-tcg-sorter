"""camera_client: stills come from the camera service when it's running, fall
back to opening the camera directly when it isn't, and service errors are
reported rather than masked by a fallback that would find the camera busy."""
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import camera_client  # noqa: E402


@pytest.fixture
def service():
    """A stand-in camera service; set .status/.body for the /capture reply."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(server.status)
            self.send_header("Content-Length", str(len(server.body)))
            self.end_headers()
            self.wfile.write(server.body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.status, server.body = 200, b"\xff\xd8jpeg"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with mock.patch.object(camera_client, "CAMERA_SERVICE_URL", f"http://127.0.0.1:{server.server_port}"):
        yield server
    server.shutdown()


def _closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_capture_comes_from_service(service, tmp_path):
    out = tmp_path / "raw.jpg"
    with mock.patch.object(camera_client, "_capture_direct") as direct:
        assert camera_client.capture_to(str(out)) == "service"
    assert out.read_bytes() == b"\xff\xd8jpeg"
    assert not direct.called


def test_service_error_is_raised_not_masked(service, tmp_path):
    service.status, service.body = 503, b'{"ok": false, "error": "timed out"}'
    with mock.patch.object(camera_client, "_capture_direct") as direct:
        with pytest.raises(camera_client.CameraServiceError, match="timed out"):
            camera_client.capture_to(str(tmp_path / "raw.jpg"))
    assert not direct.called


def test_falls_back_to_direct_capture_without_service(tmp_path):
    out = str(tmp_path / "raw.jpg")
    with mock.patch.object(camera_client, "CAMERA_SERVICE_URL", f"http://127.0.0.1:{_closed_port()}"), \
         mock.patch.object(camera_client, "_capture_direct") as direct:
        assert camera_client.capture_to(out) == "direct"
    direct.assert_called_once_with(out)
