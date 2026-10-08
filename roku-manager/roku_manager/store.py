"""Persistent configuration: settings, known TVs and schedules (one JSON file)."""

import copy
import json
import os
import re
import threading
import uuid

JELLYFIN_APP_ID = "592369"

DEFAULT_SETTINGS = {
    "status_poll_seconds": 15,
    "discovery_subnet": "",
    "target_app": {"id": JELLYFIN_APP_ID, "name": "Jellyfin"},
    "keepawake": {
        "enabled": True,
        "interval_minutes": 30,
        "keys": ["VolumeDown", "VolumeUp"],
        # "any": ping whenever the TV is on; "target_app": only while the target app is open
        "only_when": "any",
        # "leave_off" | "power_on" | "power_on_launch"
        "when_off": "leave_off",
        # "leave" | "launch_target"
        "when_other_app": "leave",
    },
}

# Remote buttons allowed in the keep-awake sequence. Power and Home are left
# out on purpose: a keep-awake press must never change what the TV is doing.
KEEPALIVE_KEYS = {
    "VolumeDown", "VolumeUp", "VolumeMute", "Info", "Up", "Down", "Left",
    "Right", "Select", "Back", "Backspace", "Search", "Enter", "Play", "Rev",
    "Fwd", "InstantReplay", "ChannelUp", "ChannelDown",
}

SCHEDULE_ACTIONS = {
    "power_on": "Turn on",
    "power_on_launch": "Turn on and open target app",
    "power_off": "Turn off",
    "launch_target": "Open target app",
    "home": "Go to Home screen",
    "input_hdmi1": "Switch to HDMI 1",
    "input_hdmi2": "Switch to HDMI 2",
    "input_hdmi3": "Switch to HDMI 3",
    "input_hdmi4": "Switch to HDMI 4",
}

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class ValidationError(ValueError):
    pass


def _choice(value, options, field):
    if value not in options:
        raise ValidationError(f"{field} must be one of: {', '.join(sorted(options))}")
    return value


def _int(value, lo, hi, field):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
        raise ValidationError(f"{field} must be a whole number")
    if not lo <= value <= hi:
        raise ValidationError(f"{field} must be between {lo} and {hi}")
    return int(value)


def _str(value, field, max_len=100):
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be text")
    value = value.strip()
    if len(value) > max_len:
        raise ValidationError(f"{field} is too long")
    return value


def _bool(value, field):
    if not isinstance(value, bool):
        raise ValidationError(f"{field} must be true or false")
    return value


def validate_settings(current, patch):
    """Merge a partial settings update into current settings, validating it."""
    s = copy.deepcopy(current)
    if "status_poll_seconds" in patch:
        s["status_poll_seconds"] = _int(patch["status_poll_seconds"], 5, 3600, "Status refresh")
    if "discovery_subnet" in patch:
        s["discovery_subnet"] = _str(patch["discovery_subnet"], "Discovery subnet", 50)
    if "target_app" in patch:
        t = patch["target_app"]
        if not isinstance(t, dict):
            raise ValidationError("target_app must be an object")
        app_id = _str(t.get("id", ""), "Target app ID", 50)
        if not app_id:
            raise ValidationError("Target app ID is required")
        s["target_app"] = {"id": app_id, "name": _str(t.get("name", ""), "Target app name") or app_id}
    if "keepawake" in patch:
        k, kp = s["keepawake"], patch["keepawake"]
        if not isinstance(kp, dict):
            raise ValidationError("keepawake must be an object")
        if "enabled" in kp:
            k["enabled"] = _bool(kp["enabled"], "Keep-awake enabled")
        if "interval_minutes" in kp:
            k["interval_minutes"] = _int(kp["interval_minutes"], 1, 1440, "Ping interval")
        if "keys" in kp:
            keys = kp["keys"]
            if not isinstance(keys, list) or not 1 <= len(keys) <= 6:
                raise ValidationError("Keep-awake keys must be a list of 1 to 6 buttons")
            for key in keys:
                _choice(key, KEEPALIVE_KEYS, "Keep-awake key")
            k["keys"] = list(keys)
        if "only_when" in kp:
            k["only_when"] = _choice(kp["only_when"], {"any", "target_app"}, "only_when")
        if "when_off" in kp:
            k["when_off"] = _choice(kp["when_off"], {"leave_off", "power_on", "power_on_launch"}, "when_off")
        if "when_other_app" in kp:
            k["when_other_app"] = _choice(kp["when_other_app"], {"leave", "launch_target"}, "when_other_app")
    return s


def validate_schedule(data, device_ids):
    if not isinstance(data, dict):
        raise ValidationError("Schedule must be an object")
    time_ = _str(data.get("time", ""), "Time", 5)
    if not _TIME_RE.match(time_):
        raise ValidationError("Time must be HH:MM (24-hour)")
    days = data.get("days")
    if not isinstance(days, list) or not days or any(
        isinstance(d, bool) or not isinstance(d, int) or not 0 <= d <= 6 for d in days
    ):
        raise ValidationError("Pick at least one day")
    devices = data.get("devices", "all")
    if devices != "all":
        if not isinstance(devices, list) or not devices:
            raise ValidationError("Pick at least one TV, or all TVs")
        unknown = [d for d in devices if d not in device_ids]
        if unknown:
            raise ValidationError(f"Unknown TV: {unknown[0]}")
    action = _choice(data.get("action"), set(SCHEDULE_ACTIONS), "Action")
    return {
        "name": _str(data.get("name", ""), "Name") or SCHEDULE_ACTIONS[action],
        "enabled": _bool(data.get("enabled", True), "Enabled"),
        "time": time_,
        "days": sorted(set(days)),
        "devices": devices,
        "action": action,
    }


def _deep_merge(base, extra):
    out = copy.deepcopy(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class Store:
    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()
        self._data = {"settings": copy.deepcopy(DEFAULT_SETTINGS), "devices": [], "schedules": []}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                saved = json.load(f)
            self._data["settings"] = _deep_merge(DEFAULT_SETTINGS, saved.get("settings", {}))
            self._data["devices"] = saved.get("devices", [])
            self._data["schedules"] = saved.get("schedules", [])

    def _save(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2)
        os.replace(tmp, self.path)

    # Settings
    def settings(self):
        with self._lock:
            return copy.deepcopy(self._data["settings"])

    def update_settings(self, patch):
        with self._lock:
            self._data["settings"] = validate_settings(self._data["settings"], patch)
            self._save()
            return self.settings()

    # Devices
    def devices(self):
        with self._lock:
            return copy.deepcopy(self._data["devices"])

    def device(self, device_id):
        with self._lock:
            for d in self._data["devices"]:
                if d["id"] == device_id:
                    return copy.deepcopy(d)
        return None

    def upsert_device_from_info(self, host, info):
        """Add a newly found TV, or refresh the address of one we already know.

        Returns (device, created).
        """
        device_id = info.get("serial-number") or info.get("device-id") or host
        with self._lock:
            for d in self._data["devices"]:
                if d["id"] == device_id:
                    d["host"] = host
                    d["model"] = info.get("model-name") or d.get("model", "")
                    self._save()
                    return copy.deepcopy(d), False
            name = (
                info.get("user-device-name")
                or info.get("friendly-device-name")
                or info.get("default-device-name")
                or f"Roku {host}"
            )
            device = {
                "id": device_id,
                "name": name,
                "host": host,
                "model": info.get("model-name", ""),
                "is_tv": info.get("is-tv", "").lower() == "true",
                "keepawake_enabled": True,
                "interval_minutes": None,  # None = use the global interval
                "held_off": False,
            }
            self._data["devices"].append(device)
            self._save()
            return copy.deepcopy(device), True

    def update_device(self, device_id, patch):
        with self._lock:
            for d in self._data["devices"]:
                if d["id"] != device_id:
                    continue
                if "name" in patch:
                    name = _str(patch["name"], "Name")
                    if not name:
                        raise ValidationError("Name can't be empty")
                    d["name"] = name
                if "keepawake_enabled" in patch:
                    d["keepawake_enabled"] = _bool(patch["keepawake_enabled"], "Keep-awake")
                if "interval_minutes" in patch:
                    v = patch["interval_minutes"]
                    d["interval_minutes"] = None if v is None else _int(v, 1, 1440, "Ping interval")
                if "held_off" in patch:
                    d["held_off"] = _bool(patch["held_off"], "held_off")
                self._save()
                return copy.deepcopy(d)
        return None

    def remove_device(self, device_id):
        with self._lock:
            before = len(self._data["devices"])
            self._data["devices"] = [d for d in self._data["devices"] if d["id"] != device_id]
            for s in self._data["schedules"]:
                if isinstance(s["devices"], list) and device_id in s["devices"]:
                    s["devices"].remove(device_id)
            # A schedule whose TVs have all been removed would otherwise be invalid.
            self._data["schedules"] = [s for s in self._data["schedules"] if s["devices"]]
            self._save()
            return len(self._data["devices"]) != before

    # Schedules
    def schedules(self):
        with self._lock:
            return copy.deepcopy(self._data["schedules"])

    def schedule(self, schedule_id):
        with self._lock:
            for s in self._data["schedules"]:
                if s["id"] == schedule_id:
                    return copy.deepcopy(s)
        return None

    def _device_ids(self):
        return {d["id"] for d in self._data["devices"]}

    def add_schedule(self, data):
        with self._lock:
            sched = validate_schedule(data, self._device_ids())
            sched["id"] = uuid.uuid4().hex[:12]
            self._data["schedules"].append(sched)
            self._save()
            return copy.deepcopy(sched)

    def update_schedule(self, schedule_id, data):
        with self._lock:
            for i, s in enumerate(self._data["schedules"]):
                if s["id"] == schedule_id:
                    merged = {**s, **data}
                    sched = validate_schedule(merged, self._device_ids())
                    sched["id"] = schedule_id
                    self._data["schedules"][i] = sched
                    self._save()
                    return copy.deepcopy(sched)
        return None

    def remove_schedule(self, schedule_id):
        with self._lock:
            before = len(self._data["schedules"])
            self._data["schedules"] = [s for s in self._data["schedules"] if s["id"] != schedule_id]
            self._save()
            return len(self._data["schedules"]) != before
