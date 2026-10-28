"""
web.py
------
Tornado request handlers for the DIAL HTTP endpoints.
Mounted by Mopidy's HTTP server at /dial/.

Routes (all relative to the /dial/ mount):
  GET  /dial/ssdp/device-desc.xml   — DIAL device description
  GET  /dial/apps/YouTube            — app info (state=running)
  POST /dial/apps/YouTube            — launch request from YT app
  OPTIONS /dial/*                    — CORS preflight
"""

import time
import urllib.parse
import uuid
import socket

import tornado.web

from mopidy_dial import logger


def _local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def _device_desc_xml(friendly_name: str, local_ip: str, port: int,
                     device_uuid: str) -> str:
    return f"""<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <URLBase>http://{local_ip}:{port}/dial/</URLBase>
  <device>
    <deviceType>urn:dial-multiscreen-org:device:dial:1</deviceType>
    <friendlyName>{friendly_name}</friendlyName>
    <manufacturer>Mopidy</manufacturer>
    <modelName>Mopidy DIAL Receiver</modelName>
    <UDN>uuid:{device_uuid}</UDN>
    <serviceList>
      <service>
        <serviceType>urn:dial-multiscreen-org:service:dial:1</serviceType>
        <serviceId>urn:dial-multiscreen-org:serviceId:dial</serviceId>
        <SCPDURL>/dial/ssdp/notfound</SCPDURL>
        <controlURL>/dial/ssdp/notfound</controlURL>
        <eventSubURL>/dial/ssdp/notfound</eventSubURL>
      </service>
    </serviceList>
  </device>
</root>"""


def _app_info_xml(app_name: str, screen_id: str = "",
                  lounge_token: str = "") -> str:
    """
    Build the DIAL app info XML response.

    When session is bound (screen_id + lounge_token present):
      - state=running with full additionalData
      - App reads screenId/loungeToken and joins the lounge directly
      - No POST needed — the app never POSTs when additionalData is present

    When session not yet ready:
      - state=stopped with no additionalData
      - App will retry when it gets a 503 response
    """
    if screen_id and lounge_token:
        # device_id may differ from screen_id — it's the stable physical device
        # identifier that YouTube Music uses to recognise the receiver across sessions
        dial_device_id = DialHandler.device_id or screen_id
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<service xmlns="urn:dial-multiscreen-org:schemas:dial">
  <name>{app_name}</name>
  <options allowStop="false"/>
  <state>running</state>
  <additionalData>
    <screenId>{screen_id}</screenId>
    <deviceId>{dial_device_id}</deviceId>
    <loungeToken>{lounge_token}</loungeToken>
    <loungeTokenRefreshIntervalMs>1800000</loungeTokenRefreshIntervalMs>
    <capabilities>dsp,que,mus</capabilities>
    <mdxVersion>3</mdxVersion>
  </additionalData>
</service>"""
    else:
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<service xmlns="urn:dial-multiscreen-org:schemas:dial">
  <name>{app_name}</name>
  <options allowStop="false"/>
  <state>stopped</state>
</service>"""



def _status_html(friendly_name: str, local_ip: str, port: int) -> str:
    s = DialHandler.state
    sc = DialHandler.pairing_code
    screen_id = DialHandler.screen_id
    lounge_token = DialHandler.lounge_token

    state_colour = {
        "idle":      "#888",
        "buffering": "#f0a500",
        "playing":   "#4caf50",
        "paused":    "#2196f3",
    }.get(s["playback_state"], "#888")

    remote_info = ""
    if s["connected"] and s["remote_name"]:
        remote_info = f"""
        <div class="row">
          <span class="label">Remote</span>
          <span class="value">{s["remote_name"]} ({s["remote_app"]})</span>
        </div>"""

    video_info = ""
    if s["video_id"]:
        yt_url = f"https://www.youtube.com/watch?v={s['video_id']}"
        mins, secs = divmod(int(s["current_time"]), 60)
        dur_mins, dur_secs = divmod(int(s["duration"]), 60)
        video_info = f"""
        <div class="row">
          <span class="label">Video</span>
          <span class="value"><a href="{yt_url}" target="_blank">{s["video_id"]}</a></span>
        </div>
        <div class="row">
          <span class="label">Position</span>
          <span class="value">{mins}:{secs:02d} / {dur_mins}:{dur_secs:02d}</span>
        </div>"""

    volume_bar = f"""
        <div class="row">
          <span class="label">Volume</span>
          <span class="value">
            <div class="vol-bar">
              <div class="vol-fill" style="width:{s['volume']}%"></div>
            </div>
            {s["volume"]}%{"  🔇" if s["muted"] else ""}
          </span>
        </div>"""

    token_section = ""
    if lounge_token:
        token_short = lounge_token[:24] + "…"
        token_section = f"""
        <div class="row">
          <span class="label">Lounge Token</span>
          <span class="value mono" title="{lounge_token}">{token_short}</span>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="5">
  <title>{friendly_name} — DIAL Status</title>
  <style>
    body {{ font-family: system-ui, sans-serif; background: #1a1a2e; color: #e0e0e0;
            margin: 0; padding: 2rem; }}
    h1   {{ font-size: 1.4rem; color: #fff; margin: 0 0 0.25rem; }}
    .sub {{ color: #888; font-size: 0.85rem; margin-bottom: 2rem; }}
    .card {{ background: #16213e; border-radius: 12px; padding: 1.5rem;
             margin-bottom: 1.5rem; box-shadow: 0 2px 8px #0005; }}
    .card h2 {{ font-size: 0.75rem; text-transform: uppercase; letter-spacing: .1em;
                color: #888; margin: 0 0 1rem; }}
    .row  {{ display: flex; justify-content: space-between; align-items: baseline;
             padding: 0.35rem 0; border-bottom: 1px solid #ffffff0d; }}
    .row:last-child {{ border-bottom: none; }}
    .label {{ color: #888; font-size: 0.85rem; flex-shrink: 0; margin-right: 1rem; }}
    .value {{ color: #fff; font-size: 0.9rem; text-align: right; word-break: break-all; }}
    .mono  {{ font-family: monospace; font-size: 0.8rem; }}
    .badge {{ display: inline-block; padding: 0.2rem 0.6rem; border-radius: 99px;
              font-size: 0.75rem; font-weight: 600; background: {state_colour};
              color: #fff; text-transform: uppercase; }}
    .pin   {{ font-size: 2.5rem; font-weight: 700; letter-spacing: 0.25em;
              color: #fff; text-align: center; padding: 1rem 0 0.5rem; }}
    .pin-hint {{ text-align: center; color: #888; font-size: 0.8rem; }}
    .vol-bar {{ display: inline-block; width: 80px; height: 6px;
                background: #333; border-radius: 3px; vertical-align: middle;
                margin-right: 0.5rem; }}
    .vol-fill {{ height: 100%; background: #4caf50; border-radius: 3px; }}
    a {{ color: #90caf9; }}
  </style>
</head>
<body>
  <h1>{friendly_name}</h1>
  <div class="sub">Mopidy DIAL Receiver · {local_ip}:{port} · refreshes every 5 s</div>

  <div class="card">
    <h2>Pairing</h2>
    <div class="pin">{sc or "—"}</div>
    <div class="pin-hint">Enter in YouTube → Settings → Watch on TV</div>
    <div class="row" style="margin-top:1rem">
      <span class="label">Screen ID</span>
      <span class="value mono">{screen_id or "—"}</span>
    </div>
    {token_section}
  </div>

  <div class="card">
    <h2>Session</h2>
    <div class="row">
      <span class="label">Status</span>
      <span class="value"><span class="badge">{s["playback_state"]}</span></span>
    </div>
    {remote_info}
    {video_info}
    {volume_bar}
  </div>
</body>
</html>"""


class DialHandler(tornado.web.RequestHandler):
    """
    Single Tornado handler covering all DIAL endpoints.
    `config` and `core` are injected by Mopidy via `initialize()`.
    `on_launch` is set by DialFrontend after the handler is created.
    """

    # Shared across all handler instances (set by DialFrontend.on_start)
    on_launch = None          # called when app sends loungeToken directly
    on_pairing_code = None    # called when app sends pairingCode (YouTube Music)
    screen_id = ""
    device_id = ""            # physical device ID — separate from screen_id
    lounge_token = ""
    pairing_code = ""
    # Time of the last pairingCode POST (YouTube Music launch flow)
    last_pairing_launch = 0.0
    PAIRING_LAUNCH_WINDOW = 120   # seconds the app is reported as running
    device_uuid = str(uuid.uuid4())

    # Current session/playback state — updated by LoungeHandler
    state = {
        "connected": False,
        "remote_name": None,
        "remote_app": None,
        "video_id": "",
        "list_id": "",
        "playback_state": "idle",   # idle | buffering | playing | paused
        "current_time": 0.0,
        "duration": 0.0,
        "volume": 50,
        "muted": False,
    }

    def initialize(self, config, core):
        self.core = core
        self.config = config
        self.local_ip = _local_ip()
        self.port = config["http"]["port"]
        self.friendly_name = config.get("dial", {}).get("friendly_name", "Mopidy")

    def set_default_headers(self):
        self.set_header("Access-Control-Allow-Origin", "*")
        self.set_header("Access-Control-Expose-Headers",
                        "Application-URL, Location")

    def options(self, path):
        """CORS preflight."""
        self.set_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.set_header("Access-Control-Allow-Headers", "Content-Type")
        self.set_status(204)
        self.finish()

    def get(self, path):
        path = path.strip("/")
        # Log all headers so we can see exactly what YouTube Music sends
        ua = self.request.headers.get("User-Agent", "")
        origin = self.request.headers.get("Origin", "")
        logger.debug(f"DIAL GET /{path}  UA={ua!r}  Origin={origin!r}")

        if path == "" or path == "index.html":
            self.set_header("Content-Type", "text/html; charset=utf-8")
            self.write(_status_html(
                friendly_name=self.friendly_name,
                local_ip=self.local_ip,
                port=self.port,
            ))
            return

        if path == "ssdp/device-desc.xml":
            xml = _device_desc_xml(
                self.friendly_name, self.local_ip, self.port,
                DialHandler.device_uuid,
            )
            self.set_header("Content-Type", "application/xml")
            # This header tells the app where to POST launch requests
            app_url = f"http://{self.local_ip}:{self.port}/dial/apps/"
            self.set_header("Application-URL", app_url)
            self.set_header("X-Application-URL", app_url)
            self.write(xml)

        elif path.startswith("apps/"):
            app_name = path.split("apps/", 1)[1].rstrip("/")
            if not DialHandler.screen_id or not DialHandler.lounge_token:
                # Session not ready yet — return stopped state with no additionalData.
                # Never return 503: the Music app caches the failure and gives up.
                logger.debug("DIAL GET /apps/* before session ready — returning stopped")
                self.set_header("Content-Type", "application/xml")
                self.write("""<?xml version="1.0" encoding="UTF-8"?>
<service xmlns="urn:dial-multiscreen-org:schemas:dial">
  <name>YouTube</name>
  <options allowStop="false"/>
  <state>stopped</state>
</service>""")
                return
            # YouTube Music ignores the "running + additionalData" shortcut
            # that the YouTube app accepts. It needs to see the app stopped,
            # POST a pairingCode, and then find us via that registered code.
            is_music = "youtube.music" in ua.lower()
            recently_launched = (
                time.time() - DialHandler.last_pairing_launch
                < DialHandler.PAIRING_LAUNCH_WINDOW
            )
            if is_music and not recently_launched:
                logger.debug(f"DIAL GET /apps/{app_name} (YT Music) -> stopped")
                self.set_header("Content-Type", "application/xml")
                self.write(_app_info_xml(app_name))
                return
            if is_music:
                # Launched: running, but no additionalData — that belongs to
                # the YouTube screen, not the Music one.
                self.set_header("Content-Type", "application/xml")
                self.write(_app_info_xml(app_name).replace(
                    "<state>stopped</state>", "<state>running</state>"))
                return
            xml = _app_info_xml(app_name, DialHandler.screen_id,
                                   DialHandler.lounge_token)
            logger.debug(f"DIAL GET /apps/{app_name} -> running, "
                         f"screenId={DialHandler.screen_id!r}")
            self.set_header("Content-Type", "application/xml")
            self.write(xml)

        else:
            self.set_status(404)
            self.finish()

    def post(self, path):
        path = path.strip("/")
        ua = self.request.headers.get("User-Agent", "")
        logger.debug(f"DIAL POST /{path}  UA={ua!r}")

        raw_body = self.request.body.decode("utf-8", errors="replace")
        logger.debug(f"DIAL POST headers: {dict(self.request.headers)}")
        logger.debug(f"DIAL POST body: {raw_body[:300]}")

        params = urllib.parse.parse_qs(raw_body)

        # The YouTube app sends loungeIdToken; some clients send loungeToken
        lounge_token = (
            params.get("loungeIdToken", params.get("loungeToken", [None]))[0]
        )
        screen_id = params.get("screenId", [None])[0]
        pairing_code = params.get("pairingCode", [None])[0]
        theme = params.get("theme", [""])[0]
        # Current YT Music builds send theme=cl and mark themselves with
        # topic=music instead; older ones send theme=m.
        if params.get("topic", [""])[0] == "music":
            theme = "m"

        app_name = path.split("apps/", 1)[-1].split("/")[0] if "apps/" in path else "unknown"
        logger.info(f"DIAL launch from app={app_name!r} "
                    f"screenId={screen_id!r} pairingCode={pairing_code!r} "
                    f"loungeToken={'yes' if lounge_token else 'NO'}")

        if lounge_token:
            # YouTube app sends loungeIdToken directly — use it immediately
            if DialHandler.on_launch:
                DialHandler.on_launch(lounge_token, screen_id)
        elif pairing_code:
            # YouTube Music sends a pairingCode UUID — notify frontend
            DialHandler.last_pairing_launch = time.time()
            if DialHandler.on_pairing_code:
                DialHandler.on_pairing_code(pairing_code, theme)
        else:
            logger.warning("DIAL POST had neither loungeToken nor pairingCode — ignoring")

        # 201 + Location is required by the DIAL spec
        location = (
            f"http://{self.local_ip}:{self.port}/dial/{path}/run"
        )
        self.set_status(201)
        self.set_header("Location", location)
        self.finish()
