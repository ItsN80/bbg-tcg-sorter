"""LEDController is a client of led_service.py (which drives the strip with
rpi_ws281x). These tests run it against a fake service on a temp Unix socket."""
import json
import os
import socket
import sys
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import led_controller  # noqa: E402

WARM = {"enabled": True, "gpio": 12, "count": 8, "brightness": 0.6,
        "color": {"r": 255, "g": 240, "b": 184}}


class FakeService:
    def __init__(self, path, reply=None):
        self.path = path
        self.requests = []
        self.active = 0
        self.max_active = 0
        self.reply = reply or {"ok": True}
        self._guard = threading.Lock()
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(path)
        self.server.listen(4)
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        with conn, conn.makefile("rwb") as f:
            for line in f:
                with self._guard:
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                time.sleep(0.002)
                self.requests.append(json.loads(line))
                with self._guard:
                    self.active -= 1
                f.write((json.dumps(self.reply) + "\n").encode())
                f.flush()

    def close(self):
        self.server.close()


def _wait(pred, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def test_sends_configured_colour_and_off(tmp_path):
    svc = FakeService(str(tmp_path / "led.sock"))
    ctl = led_controller.LEDController(WARM, socket_path=svc.path)
    try:
        assert _wait(lambda: svc.requests)
        first = svc.requests[0]
        assert first == {"enabled": True, "color": {"r": 255, "g": 240, "b": 184},
                         "brightness": 0.6, "gpio": 12, "count": 8}
        assert ctl.is_available() and ctl.get_error() == ""
        ctl.off()
        assert svc.requests[-1]["enabled"] is False
        assert ctl.get_state()["enabled"] is False
    finally:
        ctl.close()
        svc.close()


def test_frames_never_overlap(tmp_path):
    svc = FakeService(str(tmp_path / "led.sock"))
    ctl = led_controller.LEDController(WARM, socket_path=svc.path)
    try:
        def hammer(fn):
            for _ in range(15):
                fn()
        threads = [threading.Thread(target=hammer, args=(ctl.off,)),
                   threading.Thread(target=hammer, args=(lambda: ctl.set_color(255, 240, 184),)),
                   threading.Thread(target=hammer, args=(lambda: ctl._render(True, (255, 240, 184), 1.0),))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        time.sleep(0.2)
        assert len(svc.requests) > 20
        assert svc.max_active == 1
    finally:
        ctl.close()
        svc.close()


def test_service_down_reports_error_and_recovers(tmp_path):
    path = str(tmp_path / "led.sock")
    ctl = led_controller.LEDController(WARM, socket_path=path)
    try:
        assert _wait(lambda: ctl.get_error() != "")
        assert not ctl.is_available()
        assert "bbg-led.service" in ctl.get_error()
        svc = FakeService(path)  # service comes up later
        ctl.set_color(10, 20, 30)
        assert _wait(lambda: any(r["color"] == {"r": 10, "g": 20, "b": 30} for r in svc.requests), 4.0)
        assert _wait(ctl.is_available)
        svc.close()
    finally:
        ctl.close()


def test_service_error_is_surfaced(tmp_path):
    svc = FakeService(str(tmp_path / "led.sock"), reply={"ok": False, "error": "GPIO 5 is not a PWM pin"})
    ctl = led_controller.LEDController(dict(WARM, gpio=5), socket_path=svc.path)
    try:
        assert _wait(lambda: "not a PWM pin" in ctl.get_error())
        assert not ctl.is_available()
    finally:
        ctl.close()
        svc.close()
