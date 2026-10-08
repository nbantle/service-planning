import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from roku_manager.discovery import parse_ssdp_location  # noqa: E402
from roku_manager.ecp import EcpClient, EcpError  # noqa: E402
from roku_manager.engine import Engine, describe_status  # noqa: E402
from roku_manager.store import Store, ValidationError  # noqa: E402
from roku_manager.web import make_server  # noqa: E402
from tests.fake_roku import FakeRoku  # noqa: E402


class FakeClock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "config.json"))
        self.roku = FakeRoku()
        self.clock = FakeClock()
        self.now = datetime(2026, 10, 11, 8, 0)  # a Sunday
        self.engine = Engine(
            self.store,
            client_factory=lambda host: EcpClient("127.0.0.1", self.roku.port, timeout=2),
            clock=self.clock,
            local_now=lambda: self.now,
            sleep=lambda s: None,
        )
        info = EcpClient("127.0.0.1", self.roku.port).device_info()
        self.device, _ = self.store.upsert_device_from_info("127.0.0.1", info)

    def tearDown(self):
        self.engine.stop()
        self.roku.close()
        self.tmp.cleanup()

    def drain(self):
        """Wait for background work submitted by the engine to finish."""
        import time
        deadline = time.monotonic() + 10
        while self.engine._inflight and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(self.engine._inflight, "background work did not finish")


class EcpTests(Base):
    def test_device_info_and_active_app(self):
        c = EcpClient("127.0.0.1", self.roku.port)
        self.assertEqual(c.device_info()["serial-number"], "X001")
        self.assertEqual(c.active_app()["id"], "592369")
        self.assertEqual(c.media_player()["state"], "play")

    def test_403_gives_settings_hint(self):
        self.roku.forbidden = True
        with self.assertRaises(EcpError) as cm:
            EcpClient("127.0.0.1", self.roku.port).device_info()
        self.assertEqual(cm.exception.status, 403)
        self.assertIn("Control by mobile apps", str(cm.exception))

    def test_unreachable(self):
        with self.assertRaises(EcpError):
            EcpClient("127.0.0.1", 1, timeout=0.5).device_info()

    def test_ssdp_location(self):
        packet = b"HTTP/1.1 200 OK\r\nST: roku:ecp\r\nLOCATION: http://192.168.1.50:8060/\r\nUSN: uuid:roku:ecp:X\r\n\r\n"
        self.assertEqual(parse_ssdp_location(packet), ("192.168.1.50", 8060))


class StatusTests(unittest.TestCase):
    def test_hdmi(self):
        s = describe_status({"power-mode": "PowerOn"}, {"id": "tvinput.hdmi1", "name": "HDMI 1"}, None, "592369")
        self.assertEqual((s["activity_kind"], s["activity"]), ("input", "HDMI 1"))

    def test_renamed_hdmi(self):
        s = describe_status({"power-mode": "PowerOn"}, {"id": "tvinput.hdmi2", "name": "Xbox"}, None, "592369")
        self.assertEqual(s["activity"], "HDMI 2 (Xbox)")

    def test_standby(self):
        s = describe_status({"power-mode": "DisplayOff"}, None, None, "592369")
        self.assertEqual(s["power"], "standby")

    def test_target_playing(self):
        s = describe_status({"power-mode": "PowerOn"}, {"id": "592369", "name": "Jellyfin"}, {"state": "play"}, "592369")
        self.assertTrue(s["on_target"])
        self.assertEqual(s["playback"], "Playing")

    def test_home_with_screensaver(self):
        app = {"id": None, "name": "Roku", "screensaver": {"id": "55545", "name": "City"}}
        s = describe_status({"power-mode": "PowerOn"}, app, None, "592369")
        self.assertEqual(s["activity"], "Screensaver over Home screen")


class KeepAwakeTests(Base):
    def test_pings_when_on(self):
        result = self.engine.keep_awake(self.device)
        self.assertEqual(self.roku.presses, ["VolumeDown", "VolumeUp"])
        self.assertTrue(result.startswith("Pinged"))

    def test_never_wakes_an_off_tv_by_default(self):
        self.roku.power_mode = "DisplayOff"
        result = self.engine.keep_awake(self.device)
        self.assertEqual(self.roku.presses, [])
        self.assertIn("Left off", result)

    def test_manual_ping_never_wakes_tv_even_if_power_on_mode(self):
        self.store.update_settings({"keepawake": {"when_off": "power_on"}})
        self.roku.power_mode = "Ready"
        self.engine.run_command(self.device, "ping")
        self.assertEqual(self.roku.presses, [])

    def test_power_on_mode(self):
        self.store.update_settings({"keepawake": {"when_off": "power_on_launch"}})
        self.roku.power_mode = "DisplayOff"
        self.engine.keep_awake(self.device)
        self.assertEqual(self.roku.presses, ["PowerOn"])
        self.assertEqual(self.roku.launches, ["592369"])

    def test_power_on_mode_respects_deliberate_power_off(self):
        self.store.update_settings({"keepawake": {"when_off": "power_on"}})
        self.engine.run_command(self.device, "power_off")
        self.roku.presses.clear()
        self.engine.keep_awake(self.device)
        self.assertEqual(self.roku.presses, [])
        self.engine.run_command(self.device, "power_on")
        self.assertFalse(self.store.device(self.device["id"])["held_off"])

    def test_only_when_target_app(self):
        self.store.update_settings({"keepawake": {"only_when": "target_app"}})
        self.roku.app = ("tvinput.hdmi1", "HDMI 1", "tvin")
        result = self.engine.keep_awake(self.device)
        self.assertEqual(self.roku.presses, [])
        self.assertIn("isn't open", result)

    def test_switch_back_to_target(self):
        self.store.update_settings({"keepawake": {"when_other_app": "launch_target"}})
        self.roku.app = ("tvinput.hdmi1", "HDMI 1", "tvin")
        self.engine.keep_awake(self.device)
        self.assertEqual(self.roku.launches, ["592369"])

    def test_interval_timing(self):
        self.engine.tick()
        self.drain()
        self.assertEqual(self.roku.presses, [])  # still in startup grace period
        self.clock.t += 20
        self.engine.tick()
        self.drain()
        self.assertEqual(len(self.roku.presses), 2)
        self.clock.t += 29 * 60
        self.engine.tick()
        self.drain()
        self.assertEqual(len(self.roku.presses), 2)  # not due yet
        self.clock.t += 61
        self.engine.tick()
        self.drain()
        self.assertEqual(len(self.roku.presses), 4)

    def test_per_device_interval_and_disable(self):
        self.store.update_device(self.device["id"], {"interval_minutes": 5})
        d = self.store.device(self.device["id"])
        self.engine._mark_pinged(d["id"], self.clock.t)
        self.assertEqual(self.engine.next_ping_at(d, self.store.settings()), self.clock.t + 300)
        self.store.update_device(d["id"], {"keepawake_enabled": False})
        self.assertIsNone(self.engine.next_ping_at(self.store.device(d["id"]), self.store.settings()))


class ScheduleTests(Base):
    def test_fires_once_on_matching_minute(self):
        self.roku.power_mode = "DisplayOff"
        self.store.add_schedule({"time": "08:00", "days": [6], "action": "power_on", "devices": "all"})
        self.engine.check_schedules()
        self.engine.check_schedules()
        self.drain()
        self.assertEqual(self.roku.presses, ["PowerOn"])

    def test_skips_other_days(self):
        self.store.add_schedule({"time": "08:00", "days": [0, 1], "action": "power_off", "devices": "all"})
        self.engine.check_schedules()
        self.drain()
        self.assertEqual(self.roku.presses, [])

    def test_validation(self):
        with self.assertRaises(ValidationError):
            self.store.add_schedule({"time": "25:00", "days": [1], "action": "power_on"})
        with self.assertRaises(ValidationError):
            self.store.add_schedule({"time": "08:00", "days": [], "action": "power_on"})
        with self.assertRaises(ValidationError):
            self.store.add_schedule({"time": "08:00", "days": [1], "action": "power_on", "devices": ["nope"]})

    def test_removing_device_cleans_schedules(self):
        self.store.add_schedule({"time": "08:00", "days": [1], "action": "power_on", "devices": [self.device["id"]]})
        self.store.remove_device(self.device["id"])
        self.assertEqual(self.store.schedules(), [])


class SettingsTests(Base):
    def test_rejects_power_key_in_keepalive(self):
        with self.assertRaises(ValidationError):
            self.store.update_settings({"keepawake": {"keys": ["PowerOff"]}})

    def test_persists(self):
        self.store.update_settings({"keepawake": {"interval_minutes": 45}})
        again = Store(self.store.path)
        self.assertEqual(again.settings()["keepawake"]["interval_minutes"], 45)
        self.assertEqual(len(again.devices()), 1)


class ApiTests(Base):
    def setUp(self):
        super().setUp()
        self.server = make_server("127.0.0.1", 0, self.store, self.engine, discover=lambda m, s: [])
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def call(self, method, path, body=None, content_type="application/json"):
        if body is None and method != "GET":
            body = {}
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", content_type)
        try:
            with self.opener.open(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_page_and_state(self):
        with self.opener.open(self.base + "/") as r:
            self.assertIn(b"Roku TV Manager", r.read())
        status, state = self.call("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(state["devices"][0]["name"], "Lobby TV")

    def test_command(self):
        status, r = self.call("POST", f"/api/devices/{self.device['id']}/command", {"command": "input_hdmi1"})
        self.assertEqual((status, r["message"]), (200, "Switched to HDMI 1"))
        self.assertEqual(self.roku.presses, ["InputHDMI1"])

    def test_rejects_non_json_posts(self):
        status, _ = self.call("POST", f"/api/devices/{self.device['id']}/command", {"command": "power_off"}, "text/plain")
        self.assertEqual(status, 415)
        self.assertEqual(self.roku.presses, [])

    def test_bad_settings(self):
        status, r = self.call("PUT", "/api/settings", {"keepawake": {"interval_minutes": 0}})
        self.assertEqual(status, 400)
        self.assertIn("between", r["error"])

    def test_schedule_crud(self):
        status, s = self.call("POST", "/api/schedules", {"time": "13:00", "days": [6], "action": "power_off"})
        self.assertEqual(status, 200)
        status, s2 = self.call("PUT", f"/api/schedules/{s['id']}", {"enabled": False})
        self.assertFalse(s2["enabled"])
        status, _ = self.call("DELETE", f"/api/schedules/{s['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(self.store.schedules(), [])


if __name__ == "__main__":
    unittest.main()
