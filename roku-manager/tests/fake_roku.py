"""A tiny stand-in for a Roku TV's ECP server, for tests."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeRoku:
    def __init__(self, serial="X001", name="Lobby TV", host="127.0.0.1", port=0):
        self.serial = serial
        self.name = name
        self.power_mode = "PowerOn"
        self.app = ("592369", "Jellyfin", "appl")  # (id, name, type); id None = home screen
        self.player_state = "play"
        self.forbidden = False
        self.presses = []
        self.launches = []
        self._server = ThreadingHTTPServer((host, port), self._handler())
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self):
        self._server.shutdown()
        self._server.server_close()

    def _handler(self):
        roku = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, body=b"", status=200):
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if roku.forbidden:
                    return self._reply(status=403)
                if self.path == "/query/device-info":
                    xml = (
                        f"<device-info><serial-number>{roku.serial}</serial-number>"
                        f"<user-device-name>{roku.name}</user-device-name>"
                        "<model-name>TCL 55S455</model-name><is-tv>true</is-tv>"
                        f"<power-mode>{roku.power_mode}</power-mode></device-info>"
                    )
                elif self.path == "/query/active-app":
                    app_id, name, type_ = roku.app
                    if app_id:
                        xml = f'<active-app><app id="{app_id}" type="{type_}" version="1">{name}</app></active-app>'
                    else:
                        xml = "<active-app><app>Roku</app></active-app>"
                elif self.path == "/query/media-player":
                    xml = f'<player error="false" state="{roku.player_state}"/>'
                elif self.path == "/query/apps":
                    xml = (
                        '<apps><app id="592369" type="appl" version="2">Jellyfin</app>'
                        '<app id="tvinput.hdmi1" type="tvin" version="1">HDMI 1</app></apps>'
                    )
                else:
                    return self._reply(status=404)
                self._reply(xml.encode())

            def do_POST(self):
                if roku.forbidden:
                    return self._reply(status=403)
                kind, _, arg = self.path.lstrip("/").partition("/")
                if kind == "keypress":
                    roku.presses.append(arg)
                    if arg == "PowerOn":
                        roku.power_mode = "PowerOn"
                    elif arg == "PowerOff":
                        roku.power_mode = "DisplayOff"
                    elif arg.startswith("InputHDMI"):
                        n = arg[-1]
                        roku.app = (f"tvinput.hdmi{n}", f"HDMI {n}", "tvin")
                    elif arg == "Home":
                        roku.app = (None, "Roku", None)
                elif kind == "launch":
                    roku.launches.append(arg)
                    roku.app = (arg, "Jellyfin" if arg == "592369" else arg, "appl")
                else:
                    return self._reply(status=404)
                self._reply()

        return Handler
