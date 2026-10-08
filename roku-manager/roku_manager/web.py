"""HTTP server: the dashboard page plus a small JSON API."""

import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import discovery
from .ecp import EcpClient, EcpError
from .store import KEEPALIVE_KEYS, SCHEDULE_ACTIONS, ValidationError

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
MAX_BODY = 64 * 1024

DEVICE_COMMANDS = {
    "ping", "refresh", "power_on", "power_on_launch", "power_off",
    "launch_target", "home", "input_hdmi1", "input_hdmi2", "input_hdmi3", "input_hdmi4",
}


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def make_handler(store, engine, discover=discovery.discover):
    routes = []

    def route(method, pattern):
        def wrap(fn):
            routes.append((method, re.compile("^" + pattern + "$"), fn))
            return fn
        return wrap

    def device_or_404(device_id):
        device = store.device(device_id)
        if not device:
            raise ApiError(404, "TV not found")
        return device

    def device_view(d, settings):
        return {
            **d,
            "status": engine.status_of(d["id"]),
            "ping": engine.ping_info(d["id"]),
            "next_ping_at": engine.next_ping_at(d, settings),
            "effective_interval_minutes": d.get("interval_minutes") or settings["keepawake"]["interval_minutes"],
        }

    @route("GET", "/api/state")
    def get_state(body, m):
        settings = store.settings()
        return {
            "server_time": engine.clock(),
            "settings": settings,
            "devices": [device_view(d, settings) for d in store.devices()],
            "schedules": store.schedules(),
            "log": engine.recent_log(),
            "options": {
                "keepalive_keys": sorted(KEEPALIVE_KEYS),
                "schedule_actions": SCHEDULE_ACTIONS,
            },
        }

    @route("PUT", "/api/settings")
    def put_settings(body, m):
        return store.update_settings(body)

    @route("POST", "/api/discover")
    def post_discover(body, m):
        method = body.get("method", "ssdp")
        if method not in ("ssdp", "scan"):
            raise ApiError(400, "method must be ssdp or scan")
        subnet = body.get("subnet") or store.settings()["discovery_subnet"] or None
        try:
            found = discover(method, subnet)
        except (ValueError, OSError) as e:
            raise ApiError(400, f"Discovery failed: {e}")
        added = []
        for host, info in found:
            device, created = store.upsert_device_from_info(host, info)
            if created:
                added.append(device["name"])
                engine.add_log("info", f"Discovered at {host}", device)
            engine._submit(("poll", device["id"]), engine.poll_device, device)
        return {"found": len(found), "added": added}

    @route("POST", "/api/devices")
    def post_device(body, m):
        host = str(body.get("host", "")).strip()
        if not re.match(r"^[A-Za-z0-9.\-]{1,253}$", host):
            raise ApiError(400, "Enter the TV's IP address, e.g. 192.168.1.50")
        try:
            info = EcpClient(host).device_info()
        except EcpError as e:
            raise ApiError(400, str(e))
        device, created = store.upsert_device_from_info(host, info)
        engine.add_log("info", f"Added manually at {host}" if created else f"Address updated to {host}", device)
        engine._submit(("poll", device["id"]), engine.poll_device, device)
        return device

    @route("PATCH", "/api/devices/([^/]+)")
    def patch_device(body, m):
        device_or_404(m[1])
        allowed = {k: body[k] for k in ("name", "keepawake_enabled", "interval_minutes") if k in body}
        return store.update_device(m[1], allowed)

    @route("DELETE", "/api/devices/([^/]+)")
    def delete_device(body, m):
        device = device_or_404(m[1])
        store.remove_device(m[1])
        engine.add_log("info", "Removed from the app", device)
        return {"ok": True}

    @route("POST", "/api/devices/([^/]+)/command")
    def post_command(body, m):
        device = device_or_404(m[1])
        command = body.get("command")
        if command not in DEVICE_COMMANDS:
            raise ApiError(400, "Unknown command")
        try:
            return {"message": engine.run_command(device, command)}
        except EcpError as e:
            raise ApiError(502, str(e))

    @route("GET", "/api/devices/([^/]+)/apps")
    def get_apps(body, m):
        device = device_or_404(m[1])
        try:
            return {"apps": [a for a in EcpClient(device["host"]).apps() if a.get("type") == "appl"]}
        except EcpError as e:
            raise ApiError(502, str(e))

    @route("POST", "/api/schedules")
    def post_schedule(body, m):
        return store.add_schedule(body)

    @route("PUT", "/api/schedules/([^/]+)")
    def put_schedule(body, m):
        sched = store.update_schedule(m[1], body)
        if not sched:
            raise ApiError(404, "Schedule not found")
        return sched

    @route("DELETE", "/api/schedules/([^/]+)")
    def delete_schedule(body, m):
        if not store.remove_schedule(m[1]):
            raise ApiError(404, "Schedule not found")
        return {"ok": True}

    @route("POST", "/api/schedules/([^/]+)/run")
    def run_schedule(body, m):
        sched = store.schedule(m[1])
        if not sched:
            raise ApiError(404, "Schedule not found")
        return {"tvs": engine.run_schedule(sched)}

    class Handler(BaseHTTPRequestHandler):
        server_version = "RokuManager/1.0"

        def log_message(self, fmt, *args):
            pass  # keep the console for the activity log

        def _send(self, status, payload, content_type="application/json; charset=utf-8"):
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _dispatch(self, method):
            path = self.path.split("?", 1)[0]
            if method == "GET" and not path.startswith("/api/"):
                return self._static(path)
            try:
                # Requiring a JSON content type means a browser can't be tricked by
                # some other web page into sending commands here (CORS preflight).
                if method != "GET" and not (self.headers.get("Content-Type") or "").startswith("application/json"):
                    raise ApiError(415, "Send requests as application/json")
                body = {}
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    raise ApiError(413, "Request too large")
                if length:
                    body = json.loads(self.rfile.read(length))
                    if not isinstance(body, dict):
                        raise ApiError(400, "Expected a JSON object")
                for r_method, pattern, fn in routes:
                    match = pattern.match(path)
                    if match and r_method == method:
                        return self._send(200, fn(body, match))
                raise ApiError(404, "Not found")
            except ApiError as e:
                self._send(e.status, {"error": str(e)})
            except ValidationError as e:
                self._send(400, {"error": str(e)})
            except json.JSONDecodeError:
                self._send(400, {"error": "Invalid JSON"})

        def _static(self, path):
            name = "index.html" if path in ("/", "/index.html") else None
            if name is None:
                return self._send(404, {"error": "Not found"})
            with open(os.path.join(STATIC_DIR, name), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

        def do_PATCH(self):
            self._dispatch("PATCH")

        def do_DELETE(self):
            self._dispatch("DELETE")

    return Handler


def make_server(host, port, store, engine, **kwargs):
    return ThreadingHTTPServer((host, port), make_handler(store, engine, **kwargs))
