# Roku TV Manager

A small web app that watches every Roku TV on your network, keeps the ones that are on from going to sleep, and turns TVs on and off on a schedule.

- **See every TV at a glance:** on, off, or not responding, and what's showing (Jellyfin and whether it's playing, HDMI 1–4, Home screen, screensaver, another app).
- **Keep-awake pings:** at an interval you choose (globally or per TV), the app presses a harmless remote button (volume down then up, by default) so the TV registers activity and doesn't power-save.
- **A TV that's off stays off:** by default a keep-awake ping is never sent to a TV that's off, so the app never wakes a TV someone turned off. Other options can turn TVs back on, or bring them back to Jellyfin when they've been switched to something else.
- **Schedules:** turn TVs on or off, open Jellyfin, or switch inputs at set times on set days.
- **Manual controls:** ping, power, open Jellyfin, switch inputs, and Home, from any phone or computer on the network.
- **Activity log** of everything the app saw and did.

It uses Roku's built-in local control API (ECP, port 8060). Nothing goes through the internet.

## What you need

- A computer that stays on and is on the **same network as the TVs**. The Jellyfin server is a good choice.
- **Python 3.9 or newer.** No other packages are needed.
  - Windows: install from python.org and tick "Add Python to PATH".
  - Linux / macOS: usually already installed (`python3 --version`).

## One-time setup on each Roku TV

1. **Settings → System → Power → Fast TV start → On.** This lets the TV answer the app while it's off, so the app can report "Off" instead of "Not responding", and turn it on from a schedule.
2. **Settings → System → Advanced system settings → Control by mobile apps → Network access → Default.** If the app shows a "refused the request (HTTP 403)" message, set this to **Permissive**.
3. Optional but recommended: in your router, give each TV a **reserved IP address**. If a TV's address changes anyway, click **Search the network** again; the app recognizes TVs by serial number and updates the address.

## Run it

```bash
cd roku-manager
python3 run.py            # Windows: py run.py
```

Then open **http://<that-computer's-IP>:8765** in a browser on any device on your network, and click **Search the network** to find your TVs. If a TV doesn't show up, use **Scan subnet**, or type its IP address and click **Add by IP** (the TV's IP is under Settings → Network → About).

Options:

```
--port 8765      dashboard port
--host 0.0.0.0   address to listen on (use 127.0.0.1 to allow only this computer)
--data PATH      where settings are saved (default: roku-manager/data/config.json)
```

## Start it automatically

**Linux (systemd).** Edit the paths and user in `roku-manager.service`, then:

```bash
sudo cp roku-manager.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now roku-manager
journalctl -u roku-manager -f      # watch the activity log
```

**Windows.** Open Task Scheduler → Create Task:
- General: "Run whether user is logged on or not".
- Triggers: "At startup".
- Actions: Program `C:\Path\To\pythonw.exe`, arguments `run.py`, "Start in" the `roku-manager` folder.
- Allow port 8765 through Windows Firewall if other devices can't open the dashboard.

## Choosing settings

| Setting | Default | Notes |
|---|---|---|
| Ping every | 30 min | Keep it shorter than whatever is putting the TV to sleep. Roku's "Auto power savings" is typically 4 hours. Each TV can override this from its card (⋯ menu). |
| Ping with these buttons | Volume down, then up | Leaves the volume where it was; the volume bar flashes for a moment. "Mute, then unmute" is the other preset. Power and Home buttons can't be used here. |
| Which TVs get pinged | Any TV that's on | Or only TVs that are showing Jellyfin. |
| When a TV is off | Leave it off | Or turn it back on (and optionally open Jellyfin). A TV you turned off from the app or with a schedule always stays off until it's turned on from the app or by a schedule. |
| When a TV is on something else | Leave it alone | Or switch it back to Jellyfin at each ping. |
| Target app | Jellyfin (592369) | Use **Pick from a TV…** to choose any installed app. |

Settings, TVs and schedules are saved in `data/config.json`. Schedules use the clock and time zone of the computer running the app.

## Good to know

- A TV that's fully unplugged, or that has Fast TV start turned off, shows as **Not responding** and can't be turned on over the network.
- The dashboard has no password. Anyone on your network who can open it can control the TVs. Don't expose port 8765 to the internet.
- Run the tests with `python3 -m unittest discover -s tests -t .` from the `roku-manager` folder.
