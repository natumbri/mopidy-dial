"""
lounge_subscriber.py
--------------------
Manages the long-poll subscribe connection to the YouTube Lounge API.
Parses incoming BrowserChannel chunks and dispatches events to a handler.

The handler must implement LoungeEventHandler (or just be a callable that
accepts (event_name: str, event_args: dict)).
"""

import json
import requests
from mopidy_dial import logger
from .lounge_session import LoungeSession, API_BASE


def _is_balanced(s: str) -> bool:
    """Return True if s is a syntactically complete JSON array (balanced brackets)."""
    if not s.startswith("[["):
        return False
    depth = 0
    in_string = False
    escape = False
    for ch in s:
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return True
    return False


def _parse_chunk(raw: str) -> list:
    """
    Parse a complete BrowserChannel chunk string into a list of
    (event_id, event_name, event_args) tuples.
    """
    raw = raw.strip()
    if not raw or not raw.startswith("[["):
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning(f"Chunk parse error: {e} | raw[:120]={raw[:120]!r}")
        return []

    events = []
    for item in data:
        try:
            event_id = item[0]
            payload = item[1]
            event_name = payload[0]
            event_args = payload[1] if len(payload) > 1 else {}
            # Some fields arrive as JSON-encoded strings (e.g. loungeStatus.devices)
            if isinstance(event_args, dict):
                for k, v in event_args.items():
                    if isinstance(v, str) and v and v[0] in ("[", "{"):
                        try:
                            event_args[k] = json.loads(v)
                        except Exception:
                            pass
            events.append((event_id, event_name, event_args))
        except Exception as e:
            logger.warning(f"Event item parse error: {e} | item={item!r}")
    return events


class LoungeSubscriber:
    def __init__(self, session: LoungeSession, handler, stop_event=None):
        """
        session    : LoungeSession — shared state updated as events arrive
        handler    : callable(event_name, event_args) or object with on_event()
        stop_event : threading.Event — set this to interrupt the subscribe loop
                     cleanly without waiting for the 65s timeout to expire
        """
        self.session = session
        self._handler = handler
        self._stop_event = stop_event

    def _dispatch(self, event_name: str, event_args):
        if callable(self._handler):
            self._handler(event_name, event_args)
        else:
            self._handler.on_event(event_name, event_args)

    def run_forever(self):
        """
        Subscribe loop. Reconnects automatically on timeout or connection
        errors. Exits on 400 (stale SID), 401 (expired token), or
        KeyboardInterrupt.
        """
        s = self.session
        url = f"{API_BASE}/bc/bind"

        while True:
            if self._stop_event and self._stop_event.is_set():
                return "stopped"

            params = {
                **s.common_query_params,
                "RID": "rpc",
                "CI": "0",
                "AID": s.aid,
                "TYPE": "xmlhttp",
            }

            try:
                with requests.get(
                    url=url,
                    params=params,
                    headers=s.common_headers,
                    stream=True,
                    timeout=35,  # shorter timeout = faster shutdown response
                ) as resp:
                    logger.debug(f"Status: {resp.status_code} {resp.reason}")

                    if resp.status_code == 400:
                        logger.warning("Got 400 — SID is stale. Rebind needed.")
                        return "rebind"
                    if resp.status_code == 401:
                        logger.error("Got 401 — lounge token expired.")
                        return "expired"

                    pending = ""

                    for raw in resp.iter_lines(decode_unicode=True):
                        if raw is None:
                            continue
                        stripped = raw.strip()

                        # Chunk-length lines are pure integers — reset buffer
                        if stripped.isdigit():
                            pending = ""
                            continue

                        # Start or continue accumulating a chunk
                        if stripped.startswith("[["):
                            pending = stripped
                        elif pending:
                            pending += "\n" + stripped

                        # Only parse when the chunk is syntactically complete
                        if pending and _is_balanced(pending):
                            events = _parse_chunk(pending)
                            pending = ""
                            for event_id, event_name, event_args in events:
                                if event_id >= s.aid:
                                    s.aid = event_id + 1
                                self._dispatch(event_name, event_args)

            except requests.exceptions.Timeout:
                if self._stop_event and self._stop_event.is_set():
                    return "stopped"
                # Normal timeout — just reconnect
            except requests.exceptions.ConnectionError as e:
                if self._stop_event and self._stop_event.is_set():
                    return "stopped"
                from mopidy_dial import logger as _log
                _log.warning(f"Lounge connection error: {e} — reconnecting")
            except KeyboardInterrupt:
                return "stopped"
