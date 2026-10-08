"""Background work: status polling, keep-awake pings and scheduled actions."""

import threading
import time
import traceback
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from .ecp import EcpClient, EcpError
from .store import SCHEDULE_ACTIONS

STARTUP_GRACE_SECONDS = 15
KEY_GAP_SECONDS = 0.4
LAUNCH_AFTER_POWER_ON_SECONDS = 6

INPUT_KEYS = {f"input_hdmi{n}": f"InputHDMI{n}" for n in range(1, 5)}


def describe_status(info, app, player, target_app_id):
    """Turn raw ECP query results into a status the UI can show."""
    power_mode = info.get("power-mode") or "PowerOn"
    status = {
        "reachable": True,
        "power": "on" if power_mode == "PowerOn" else "standby",
        "power_mode": power_mode,
        "app_id": None,
        "app_name": None,
        "activity_kind": "off",
        "activity": "Off (standby)",
        "on_target": False,
        "playback": None,
        "error": None,
    }
    if status["power"] != "on":
        return status

    if app is None:
        status.update(activity_kind="unknown", activity="On")
        return status

    app_id, name = app.get("id"), app.get("name") or ""
    status["app_id"], status["app_name"] = app_id, name
    if not app_id:
        status.update(activity_kind="home", activity="Home screen")
    elif app_id.startswith("tvinput."):
        source = app_id.split(".", 1)[1]
        default = f"HDMI {source[4:]}" if source.startswith("hdmi") else {"dtv": "Live TV", "cvbs": "AV"}.get(source, source)
        label = default if not name or name == default else f"{default} ({name})"
        status.update(activity_kind="input", activity=label)
    else:
        status.update(activity_kind="app", activity=name or app_id)
        status["on_target"] = app_id == target_app_id
        if player and player.get("state") in ("play", "pause", "buffer"):
            status["playback"] = {"play": "Playing", "pause": "Paused", "buffer": "Buffering"}[player["state"]]

    if app.get("screensaver"):
        status["activity_kind"] = "screensaver"
        status["activity"] = f"Screensaver over {status['activity']}"
    return status


def offline_status(error):
    return {
        "reachable": False,
        "power": "offline",
        "power_mode": None,
        "app_id": None,
        "app_name": None,
        "activity_kind": "offline",
        "activity": "Not responding",
        "on_target": False,
        "playback": None,
        "error": str(error),
    }


class Engine:
    def __init__(self, store, client_factory=None, clock=time.time, local_now=datetime.now, sleep=time.sleep):
        self.store = store
        self.client_factory = client_factory or EcpClient
        self.clock = clock
        self.local_now = local_now
        self.sleep = sleep
        self.started_at = clock()
        self.log = deque(maxlen=300)
        self._lock = threading.RLock()
        self._status = {}  # device id -> status dict
        self._pings = {}  # device id -> {"last_at", "last_result"}
        self._fired = {}  # schedule id -> "YYYY-MM-DD HH:MM" it last fired
        self._inflight = set()
        self._next_poll = 0.0
        self._executor = ThreadPoolExecutor(max_workers=16, thread_name_prefix="roku")
        self._stop = threading.Event()
        self._thread = None

    # ---------- lifecycle ----------

    def start(self):
        self._thread = threading.Thread(target=self._loop, name="engine", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._executor.shutdown(wait=False)

    def _loop(self):
        while not self._stop.wait(1.0):
            try:
                self.tick()
            except Exception:
                self.add_log("error", "Engine error: " + traceback.format_exc(limit=3))

    def _submit(self, key, fn, *args):
        with self._lock:
            if key in self._inflight:
                return
            self._inflight.add(key)

        def run():
            try:
                fn(*args)
            except Exception as e:
                self.add_log("error", f"{key[0]} failed: {e}")
            finally:
                with self._lock:
                    self._inflight.discard(key)

        try:
            self._executor.submit(run)
        except RuntimeError:  # shutting down
            with self._lock:
                self._inflight.discard(key)

    # ---------- log ----------

    def add_log(self, level, message, device=None):
        entry = {
            "at": self.clock(),
            "level": level,
            "device": device["name"] if device else None,
            "message": message,
        }
        with self._lock:
            self.log.append(entry)
        prefix = f"[{entry['device']}] " if device else ""
        print(f"{datetime.fromtimestamp(entry['at']):%Y-%m-%d %H:%M:%S} {level.upper():5} {prefix}{message}", flush=True)

    def recent_log(self, limit=150):
        with self._lock:
            return list(self.log)[-limit:][::-1]

    # ---------- the 1-second tick ----------

    def interval_seconds(self, device, settings):
        minutes = device.get("interval_minutes") or settings["keepawake"]["interval_minutes"]
        return minutes * 60

    def next_ping_at(self, device, settings):
        if not settings["keepawake"]["enabled"] or not device.get("keepawake_enabled"):
            return None
        with self._lock:
            last = self._pings.get(device["id"], {}).get("last_at")
        if last is None:
            return self.started_at + STARTUP_GRACE_SECONDS
        return last + self.interval_seconds(device, settings)

    def tick(self):
        now = self.clock()
        settings = self.store.settings()
        devices = self.store.devices()

        if now >= self._next_poll:
            self._next_poll = now + settings["status_poll_seconds"]
            for d in devices:
                self._submit(("poll", d["id"]), self.poll_device, d)

        for d in devices:
            due = self.next_ping_at(d, settings)
            if due is not None and now >= due:
                self._mark_pinged(d["id"], now)  # claim it so the next tick doesn't re-submit
                self._submit(("keepawake", d["id"]), self.keep_awake, d)

        self.check_schedules(devices)

    def _mark_pinged(self, device_id, at, result=None):
        with self._lock:
            entry = self._pings.setdefault(device_id, {"last_at": None, "last_result": None})
            entry["last_at"] = at
            if result is not None:
                entry["last_result"] = result

    # ---------- status ----------

    def poll_device(self, device):
        settings = self.store.settings()
        client = self.client_factory(device["host"])
        try:
            info = client.device_info()
        except EcpError as e:
            status = offline_status(e)
        else:
            app = player = None
            if (info.get("power-mode") or "PowerOn") == "PowerOn":
                try:
                    app = client.active_app()
                except EcpError:
                    pass
                if app and app.get("id") and not app["id"].startswith("tvinput."):
                    try:
                        player = client.media_player()
                    except EcpError:
                        pass
            status = describe_status(info, app, player, settings["target_app"]["id"])
        status["checked_at"] = self.clock()

        with self._lock:
            previous = self._status.get(device["id"])
            self._status[device["id"]] = status
        if previous is None:
            self.add_log("info", f"Status: {status['activity']}", device)
        elif (previous["power"], previous["activity"]) != (status["power"], status["activity"]):
            self.add_log("info", f"{previous['activity']} → {status['activity']}", device)
        return status

    def status_of(self, device_id):
        with self._lock:
            return self._status.get(device_id)

    def ping_info(self, device_id):
        with self._lock:
            return dict(self._pings.get(device_id, {"last_at": None, "last_result": None}))

    # ---------- keep-awake ----------

    def keep_awake(self, device, manual=False):
        """Decide what to do for one TV and do it. Returns a short description."""
        settings = self.store.settings()
        ka, target = settings["keepawake"], settings["target_app"]
        device = self.store.device(device["id"]) or device
        status = self.poll_device(device)  # always act on fresh state
        client = self.client_factory(device["host"])

        try:
            if not status["reachable"]:
                result = "Skipped: TV not responding"
            elif status["power"] != "on":
                if manual or ka["when_off"] == "leave_off":
                    result = "Left off (TV is off)"
                elif device.get("held_off"):
                    result = "Left off (turned off by schedule or by hand)"
                else:
                    client.keypress("PowerOn")
                    result = "TV was off — turned it on"
                    if ka["when_off"] == "power_on_launch":
                        self.sleep(LAUNCH_AFTER_POWER_ON_SECONDS)
                        client.launch(target["id"])
                        result += f" and opened {target['name']}"
            elif not status["on_target"] and ka["when_other_app"] == "launch_target" and not manual:
                client.launch(target["id"])
                result = f"Switched from {status['activity']} to {target['name']}"
            elif not status["on_target"] and ka["only_when"] == "target_app" and not manual:
                result = f"Skipped: {target['name']} isn't open ({status['activity']})"
            else:
                for i, key in enumerate(ka["keys"]):
                    if i:
                        self.sleep(KEY_GAP_SECONDS)
                    client.keypress(key)
                result = "Pinged (" + ", ".join(ka["keys"]) + ")"
        except EcpError as e:
            result = f"Failed: {e}"

        self._mark_pinged(device["id"], self.clock(), result)
        level = "error" if result.startswith("Failed") else "info"
        self.add_log(level, ("Ping now: " if manual else "Keep-awake: ") + result, device)
        return result

    # ---------- commands ----------

    def run_command(self, device, command, source="Manual"):
        settings = self.store.settings()
        target = settings["target_app"]
        client = self.client_factory(device["host"])

        if command == "ping":
            return self.keep_awake(device, manual=True)
        if command == "refresh":
            return self.poll_device(device)["activity"]

        try:
            if command == "power_on":
                client.keypress("PowerOn")
                message = "Turned on"
            elif command == "power_on_launch":
                client.keypress("PowerOn")
                self.sleep(LAUNCH_AFTER_POWER_ON_SECONDS)
                client.launch(target["id"])
                message = f"Turned on and opened {target['name']}"
            elif command == "power_off":
                client.keypress("PowerOff")
                message = "Turned off"
            elif command == "launch_target":
                client.launch(target["id"])
                message = f"Opened {target['name']}"
            elif command == "home":
                client.keypress("Home")
                message = "Went to Home screen"
            elif command in INPUT_KEYS:
                client.keypress(INPUT_KEYS[command])
                message = f"Switched to HDMI {command[-1]}"
            else:
                raise ValueError(f"Unknown command: {command}")
        except EcpError as e:
            self.add_log("error", f"{source}: {command} failed: {e}", device)
            raise

        # Remember deliberate power changes so "turn back on" keep-awake mode
        # doesn't undo a scheduled or manual power-off.
        if command == "power_off":
            self.store.update_device(device["id"], {"held_off": True})
        elif command in ("power_on", "power_on_launch"):
            self.store.update_device(device["id"], {"held_off": False})

        self.add_log("info", f"{source}: {message}", device)
        self._submit(("poll", device["id"]), self._delayed_poll, device)
        return message

    def _delayed_poll(self, device):
        self.sleep(2)
        self.poll_device(device)

    # ---------- scheduler ----------

    def check_schedules(self, devices=None):
        now = self.local_now()
        minute = now.strftime("%Y-%m-%d %H:%M")
        hhmm, weekday = now.strftime("%H:%M"), now.weekday()
        for sched in self.store.schedules():
            if not sched["enabled"] or sched["time"] != hhmm or weekday not in sched["days"]:
                continue
            with self._lock:
                if self._fired.get(sched["id"]) == minute:
                    continue
                self._fired[sched["id"]] = minute
            self.run_schedule(sched, devices)

    def run_schedule(self, sched, devices=None):
        devices = devices if devices is not None else self.store.devices()
        targets = [d for d in devices if sched["devices"] == "all" or d["id"] in sched["devices"]]
        source = f"Schedule “{sched['name']}”"
        self.add_log("info", f"{source} running: {SCHEDULE_ACTIONS[sched['action']]} on {len(targets)} TV(s)")
        for d in targets:
            self._submit(("schedule", sched["id"], d["id"]), self._run_quietly, d, sched["action"], source)
        return len(targets)

    def _run_quietly(self, device, command, source):
        try:
            self.run_command(device, command, source)
        except EcpError:
            pass  # already logged
