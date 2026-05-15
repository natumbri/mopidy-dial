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
import urllib.request
import urllib.parse

API_BASE = "https://www.youtube.com/api/lounge"
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
    def __init__(self, screen_id: str, lounge_token: str, device_name: str = "Python Receiver"):
        self.screen_id = screen_id
        self.lounge_token = lounge_token
        self.device_name = device_name

        # Populated after bind()
        self.sid: str = ""
        self.gsessionid: str = ""
        self.cookie: str = ""
        self.aid: int = 0   # last received event id
        self.rid: int = 2   # command sequence id (1 was used by initial bind)

    @classmethod
    def create(cls, device_name: str = "Python Receiver") -> "LoungeSession":
        """
        Full self-registration: generate a fresh screen_id and lounge_token
        from YouTube's servers. Use this instead of relying on frontend.py.
        """
        # 1. Generate a screen_id
        screen_id = _get(f"{API_BASE}/pairing/generate_screen_id").decode().strip()
        print(f"Generated screen_id: {screen_id}")

        # 2. Get a lounge_token for that screen_id
        raw = _post(f"{API_BASE}/pairing/get_lounge_token_batch",
                    {"screen_ids": screen_id})
        data = json.loads(raw)
        screen = data["screens"][0]
        lounge_token = screen["loungeToken"]
        print(f"Got lounge_token: {lounge_token[:40]}...")

        return cls(screen_id, lounge_token, device_name)

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
        print(f"Pairing code: {code}")

        # Step 2: register the code for both YouTube and YouTube Music.
        # Each app scans for its own app identifier, so we register both.
        self._register_pairing_code(code, app)
        self._register_pairing_code(code, "youtube-music")

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
        print(f"register_pairing_code response: {raw.decode()[:200]!r}")

    def bind(self):
        """Perform the initial bind POST to establish SID, gsessionid, and session cookie."""
        body = {
            "name": self.device_name,
            "device": "LOUNGE_SCREEN",
            "capabilities": "dsp,que,mus",
            "mdx-version": "3",
            "clientName": CLIENT_NAME,
            "clientVersion": CLIENT_VERSION,
            "count": "0",
            "screenId": self.screen_id,
            "id": self.screen_id,
            "loungeIdToken": self.lounge_token,
        }

        query = urllib.parse.urlencode({"RID": "1", "VER": "8", "CVER": "1"})
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

    @property
    def common_query_params(self) -> dict:
        """Query params included on every subsequent request."""
        return {
            "device": "LOUNGE_SCREEN",
            "id": self.screen_id,
            "name": self.device_name,
            "loungeIdToken": self.lounge_token,
            "SID": self.sid,
            "gsessionid": self.gsessionid,
            "VER": "8",
            "v": "2",
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