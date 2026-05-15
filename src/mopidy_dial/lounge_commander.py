"""
lounge_commander.py
-------------------
Sends commands from the screen back to the YouTube Lounge API.
All methods take a LoungeSession and use its current state (SID,
gsessionid, AID, RID) to build the correct request.
"""

import urllib.request
import urllib.parse
from .lounge_session import LoungeSession, API_BASE


class LoungeCommander:
    def __init__(self, session: LoungeSession):
        self.session = session

    def send(self, sc: str, params: dict = {}) -> bool:
        """
        Send a single screen command (_sc) with optional parameters.
        Returns True on success, False on failure.
        """
        s = self.session
        query = urllib.parse.urlencode({
            **s.common_query_params,
            "RID": s.rid,
            "AID": s.aid,
            "CVER": "1",
        })
        url = f"{API_BASE}/bc/bind?{query}"

        body = {"count": "1", "req0__sc": sc}
        for k, v in params.items():
            body[f"req0_{k}"] = str(v)

        payload = urllib.parse.urlencode(body).encode()
        headers = {
            **s.common_headers,
            "Content-Type": "application/x-www-form-urlencoded",
        }

        try:
            req = urllib.request.Request(url, data=payload, headers=headers)
            resp = urllib.request.urlopen(req)
            resp.read()
            s.rid += 1
            print(f"  → Sent '{sc}' OK")
            return True
        except Exception as e:
            print(f"  → Failed to send '{sc}': {e}")
            return False

    # --- Convenience wrappers ---

    def now_playing(self, video_id="", list_id="", state="-1",
                    current_time="0", duration="0", cpn=""):
        return self.send("nowPlaying", {
            "videoId": video_id,
            "listId": list_id,
            "state": state,
            "currentTime": current_time,
            "duration": duration,
            "cpn": cpn,
        })

    def state_change(self, state: str, current_time="0", duration="0",
                     video_id="", cpn=""):
        params = {"state": state, "currentTime": current_time, "duration": duration}
        if video_id:
            params["videoId"] = video_id
        if cpn:
            params["cpn"] = cpn
        return self.send("onStateChange", params)

    def volume_changed(self, volume: int, muted: bool = False):
        return self.send("onVolumeChanged", {
            "volume": volume,
            "muted": str(muted).lower(),
        })

    def discovery_device_id(self, device_id: str):
        return self.send("onDiscoveryDeviceIdRetrieved", {
            "discoveryDeviceId": device_id,
        })

    def noop(self):
        return self.send("noop")
