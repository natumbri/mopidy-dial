"""
lounge_session.py
-----------------
Manages credentials and the initial bind to the YouTube Lounge API.
Holds all session state (SID, gsessionid, cookie, AID, RID) that the
other modules share via a single LoungeSession instance.

Full screen registration flow (call these in order if not using
an external frontend.py):

    session = LoungeSession.create("My Screen")
    session.register_pairing_code()   # tells YT servers this screen exists
    code = session.get_pairing_code() # display this to the user
    session.bind()                    # establish SID/gsessionid

Or if you already have screen_id + lounge_token from frontend.py:

    session = LoungeSession(screen_id, lounge_token)
    session.register_pairing_code()
    session.bind()
"""

import re
import json
import threading
import urllib.request
import urllib.parse

from mopidy_dial import logger

API_BASE = "https://www.youtube.com/api/lounge"
MUSIC_SCREEN_APP = "ytcr"
CLIENT_NAME = "tvhtml5"
CLIENT_VERSION = "7.20230101"

SID_REGEX = r'"c","(.*?)",'
GSESSION_ID_REGEX = r'"S","(.*?)"\]'

# Match the User-Agent used by frontend.py so the server treats all
# requests as coming from the same Tizen TV client.
TIZEN_UA = (
    "Mozilla/5.0 (Linux; Tizen 2.3) AppleWebKit/538.1 "
    "(KHTML, like Gecko) TV Safari/538.1"
)
BASE_HEADERS = {
    "User-Agent": TIZEN_UA,
    "Content-Type": "application/x-www-form-urlencoded",
}


def _post(url: str, params: dict, extra_headers: dict = {}) -> bytes:
    payload = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=payload, headers={
        **BASE_HEADERS,
        **extra_headers,
    })
    resp = urllib.request.urlopen(req)
    return resp.read()


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": TIZEN_UA})
    resp = urllib.request.urlopen(req)
    return resp.read()


class LoungeSession:
    def __init__(self, screen_id: str, lounge_token: str,
                 device_name: str = "Python Receiver", theme: str = ""):
        self.screen_id = screen_id
        self.lounge_token = lounge_token
        self.device_name = device_name

        # YouTube Music only connects to a screen registered with theme "m".
        # These mirror the params yt-cast-receiver uses for that screen, and
        # are sent on every request. The YouTube session has none (theme "").
        self.theme = theme
        self.capabilities = "dsp,que,mus"
        self.mdx_version = "3"
        self.extra_params: dict = {}
        if theme == "m":
            self.capabilities = "dsp,mic,dpa,ntb"
            self.mdx_version = "2"
            self.extra_params = {
                "app": MUSIC_SCREEN_APP,
                "theme": theme,
                "capabilities": self.capabilities,
                "cst": "m",
                "mdxVersion": self.mdx_version,
                "obfuscatedGaiaId": "",
                "deviceInfo": json.dumps({
                    "brand": "Mopidy", "model": "Dial", "year": 0,
                    "os": "Linux", "osVersion": "", "chipset": "",
                    "clientName": "TVHTML5",
                    "dialAdditionalDataSupportLevel": "unsupported",
                    "mdxDialServerType": "MDX_DIAL_SERVER_TYPE_UNKNOWN",
                }),
            }

        self.device_id: str = screen_id  # may be overridden by create()

        # Populated after bind()
        self.sid: str = ""
        self.gsessionid: str = ""
        self.cookie: str = ""
        self.aid: int = 0   # last received event id
        self.rid: int = 2   # command sequence id (1 was used by initial bind)
        # Commands come from several threads; RIDs must be sent in order.
        self.send_lock = threading.Lock()
        self.ofs: int = 0   # number of messages already sent on this SID

    @classmethod
    def create(cls, device_name: str = "Python Receiver",
               persist_path: str = None, theme: str = "") -> "LoungeSession":
        """
        Full self-registration: generate or reload a screen_id and lounge_token.

        If persist_path is given, the screen_id is saved there and reused on
        subsequent calls. This is important for YouTube Music, which caches our
        screen_id — if it changes every restart, Music loses track of us.
        """
        import os

        # Try to load a persisted screen_id
        screen_id = None
        if persist_path and os.path.exists(persist_path):
            try:
                with open(persist_path) as f:
                    screen_id = f.read().strip()
                if screen_id:
                    logger.info(f"Reusing persisted screen_id: {screen_id}")
            except Exception:
                screen_id = None

        # Generate a fresh screen_id if we don't have one
        if not screen_id:
            screen_id = _get(f"{API_BASE}/pairing/generate_screen_id").decode().strip()
            logger.info(f"Generated new screen_id: {screen_id}")
            if persist_path:
                try:
                    os.makedirs(os.path.dirname(persist_path), exist_ok=True)
                    with open(persist_path, "w") as f:
                        f.write(screen_id)
                except Exception as e:
                    logger.warning(f"Could not persist screen_id: {e}")

        # device_id is a separate persistent identifier (the physical device ID).
        # It must be stable across restarts — YouTube Music uses it to recognise
        # the receiver device. We persist it alongside screen_id.
        device_id = screen_id  # default: same as screen_id for backward compat
        if persist_path:
            device_id_path = persist_path + "_device_id"
            try:
                if os.path.exists(device_id_path):
                    with open(device_id_path) as f:
                        device_id = f.read().strip() or screen_id
                else:
                    # Generate a stable device_id once
                    import uuid
                    device_id = uuid.uuid4().hex[:26]  # same length as screen_id
                    with open(device_id_path, "w") as f:
                        f.write(device_id)
                    logger.info(f"Generated new device_id: {device_id}")
            except Exception as e:
                logger.warning(f"Could not persist device_id: {e}")

        # Always get a fresh lounge_token (tokens expire, screen_id doesn't)
        raw = _post(f"{API_BASE}/pairing/get_lounge_token_batch",
                    {"screen_ids": screen_id})
        data = json.loads(raw)
        screen = data["screens"][0]
        lounge_token = screen["loungeToken"]
        logger.debug("Fetched fresh lounge token")

        session = cls(screen_id, lounge_token, device_name, theme)
        session.device_id = device_id
        return session

    def get_pairing_code(self, app: str = "youtube-desktop") -> str:
        """
        Generate a PIN code and register it with YouTube's pairing
        infrastructure so official apps can connect via /pairing/get_screen.

        Returns the code string to display to the user.

        The correct order is:
          1. get_pairing_code()        — generates AND registers the code
          2. bind()                    — open the BrowserChannel session
        """
        # Step 1: generate the pairing code
        params = {
            "access_type": "permanent",
            "app": app,
            "screen_id": self.screen_id,
            "lounge_token": self.lounge_token,
        }
        raw = _post(
            f"{API_BASE}/pairing/get_pairing_code",
            params,
            extra_headers={"X-YouTube-LoungeId-Token": self.lounge_token},
        )
        code = raw.decode().strip()
        
        # Register for all client types that might connect:
        # - youtube-leanback: standard YouTube app PIN pairing
        # - android-music: YouTube Music android app
        # - YouTubeMusic: YouTube Music DIAL app name
        self._register_pairing_code(code, app)
        self._register_pairing_code(code, "android-music")
        self._register_pairing_code(code, "YouTubeMusic")

        return code

    def _register_pairing_code(self, pairing_code: str, app: str = "youtube-desktop"):
        """Register a pairing code. Called automatically by get_pairing_code()."""
        params = {
            "access_type": "permanent",
            "app": app,
            "pairing_code": pairing_code,
            "screen_id": self.screen_id,
            "lounge_token": self.lounge_token,
            "device_id": self.screen_id,
        }
        raw = _post(
            f"{API_BASE}/pairing/register_pairing_code",
            params,
            extra_headers={"X-YouTube-LoungeId-Token": self.lounge_token},
        )
        logger.debug(f"Registered pairing code for app={app!r}: {raw.decode()[:200]!r}")

    def register_pairing_code(self, pairing_code: str):
        """
        Register a pairing code the *sender* generated (DIAL launch). The
        sender then looks this screen up by that code and connects to it.
        """
        raw = _post(f"{API_BASE}/pairing/register_pairing_code", {
            "access_type": "permanent",
            "app": self.extra_params.get("app", "ytcr"),
            "pairing_code": pairing_code,
            "screen_id": self.screen_id,
            "screen_name": self.device_name,
            "device_id": self.device_id,
        })
        logger.debug(f"Registered DIAL pairing code {pairing_code!r} "
                     f"(theme={self.theme!r}): {raw.decode()[:200]!r}")

    def bind(self):
        """Perform the initial bind POST to establish SID, gsessionid, and session cookie."""
        body = {
            "name": self.device_name,
            "device": "LOUNGE_SCREEN",
            "capabilities": self.capabilities,
            "mdx-version": self.mdx_version,
            "clientName": CLIENT_NAME,
            "clientVersion": CLIENT_VERSION,
            "count": "0",
            "screenId": self.screen_id,
            "id": self.device_id,        # physical device ID, separate from screen_id
            "loungeIdToken": self.lounge_token,
        }

        query = urllib.parse.urlencode({
            **self.extra_params, "RID": "1", "VER": "8", "CVER": "1",
        })
        url = f"{API_BASE}/bc/bind?{query}"
        payload = urllib.parse.urlencode(body).encode()

        req = urllib.request.Request(url, data=payload, headers={
            **BASE_HEADERS,
            "X-YouTube-LoungeId-Token": self.lounge_token,
        })
        resp = urllib.request.urlopen(req)
        raw = resp.read().decode("UTF-8")

        for key, val in resp.getheaders():
            if key.lower() == "set-cookie":
                self.cookie = val.split(";", 1)[0].strip()

        sid_match = re.search(SID_REGEX, raw)
        gsession_match = re.search(GSESSION_ID_REGEX, raw)

        if not sid_match or not gsession_match:
            raise RuntimeError(f"Failed to extract SID/gsessionid from bind response:\n{raw}")

        self.sid = sid_match.group(1)
        self.gsessionid = gsession_match.group(1)
        # A new SID starts a fresh event/command sequence
        self.aid = 0
        self.rid = 2
        self.ofs = 0

    @property
    def common_query_params(self) -> dict:
        """Query params included on every subsequent request."""
        return {
            "device": "LOUNGE_SCREEN",
            "id": self.device_id,        # physical device ID
            "name": self.device_name,
            "loungeIdToken": self.lounge_token,
            "SID": self.sid,
            "gsessionid": self.gsessionid,
            "VER": "8",
            "v": "2",
            **self.extra_params,
        }

    @property
    def common_headers(self) -> dict:
        return {
            "User-Agent": TIZEN_UA,
            "Cookie": self.cookie,
            "X-YouTube-LoungeId-Token": self.lounge_token,
        }

    def __repr__(self):
        return (
            f"LoungeSession(screen_id={self.screen_id!r}, "
            f"sid={self.sid!r}, gsessionid={self.gsessionid!r}, "
            f"aid={self.aid}, rid={self.rid})"
        )
