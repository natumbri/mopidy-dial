"""
lounge_commander.py
-------------------
Sends commands from the screen back to the YouTube Lounge API.
All methods take a LoungeSession and use its current state (SID,
gsessionid, AID, RID) to build the correct request.
"""

import urllib.request
import urllib.parse
from mopidy_dial import logger
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
        with s.send_lock:
            return self._send_locked(sc, params)

    def _send_locked(self, sc: str, params: dict) -> bool:
        s = self.session
        rid = s.rid
        s.rid += 1  # RIDs are consumed even if the request fails
        ofs = s.ofs
        s.ofs += 1
        query = urllib.parse.urlencode({
            **s.common_query_params,
            "RID": rid,
            "AID": s.aid,
            "CVER": "1",
        })
        url = f"{API_BASE}/bc/bind?{query}"

        body = {"count": "1", "ofs": str(ofs), "req0__sc": sc}
        for k, v in params.items():
            body[f"req0_{k}"] = str(v)

        payload = urllib.parse.urlencode(body).encode()
        headers = {
            **s.common_headers,
            "Content-Type": "application/x-www-form-urlencoded",
        }

        try:
            req = urllib.request.Request(url, data=payload, headers=headers)
            resp = urllib.request.urlopen(req, timeout=15)
            reply = resp.read()
            logger.debug(f"Sent '{sc}' OK (RID={rid} ofs={ofs}) {params} -> {resp.status} {reply[:120]!r}")
            return True
        except Exception as e:
            logger.warning(f"Failed to send '{sc}' (RID={rid}): {e}")
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
