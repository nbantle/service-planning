"""Minimal client for Roku's External Control Protocol (ECP).

Every Roku device on the LAN exposes a small HTTP API on port 8060:
  GET  /query/device-info   power state, model, serial number, ...
  GET  /query/active-app    what is in the foreground (app, HDMI input, home)
  GET  /query/media-player  playback state of the foreground app
  GET  /query/apps          installed channels
  POST /keypress/<key>      simulate a remote button press
  POST /launch/<app-id>     open a channel
"""

import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

ECP_PORT = 8060

# Never route LAN traffic through a system HTTP proxy.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

FORBIDDEN_HINT = (
    "The TV refused the request (HTTP 403). On the TV go to Settings > System > "
    "Advanced system settings > Control by mobile apps and set Network access "
    "to Default or Permissive."
)


class EcpError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class EcpClient:
    def __init__(self, host, port=ECP_PORT, timeout=3.0):
        self.host = host
        self.port = port
        self.timeout = timeout

    def _request(self, method, path):
        url = f"http://{self.host}:{self.port}{path}"
        data = b"" if method == "POST" else None
        req = urllib.request.Request(url, data=data, method=method)
        try:
            with _opener.open(req, timeout=self.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            e.close()
            if e.code == 403:
                raise EcpError(FORBIDDEN_HINT, 403) from e
            raise EcpError(f"TV returned HTTP {e.code} for {path}", e.code) from e
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise EcpError(f"Could not reach {self.host}: {reason}") from e

    def _query(self, path):
        body = self._request("GET", path)
        try:
            return ET.fromstring(body)
        except ET.ParseError as e:
            raise EcpError(f"Unreadable response from {self.host}{path}") from e

    def device_info(self):
        root = self._query("/query/device-info")
        return {child.tag: (child.text or "").strip() for child in root}

    def active_app(self):
        root = self._query("/query/active-app")
        app = root.find("app")
        saver = root.find("screensaver")
        result = {"id": None, "name": None, "type": None, "screensaver": None}
        if app is not None:
            result.update(id=app.get("id"), name=(app.text or "").strip(), type=app.get("type"))
        if saver is not None:
            result["screensaver"] = {"id": saver.get("id"), "name": (saver.text or "").strip()}
        return result

    def media_player(self):
        root = self._query("/query/media-player")
        return {"state": root.get("state")}

    def apps(self):
        root = self._query("/query/apps")
        return [
            {"id": a.get("id"), "name": (a.text or "").strip(), "type": a.get("type")}
            for a in root.findall("app")
        ]

    def keypress(self, key):
        self._request("POST", "/keypress/" + urllib.parse.quote(key, safe=""))

    def launch(self, app_id):
        self._request("POST", "/launch/" + urllib.parse.quote(app_id, safe=""))
