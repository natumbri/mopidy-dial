"""
frontend.py
-----------
Mopidy frontend actor. Responsible for:
  1. Running the SSDP listener/notifier (makes device visible on LAN)
  2. Creating and managing the Lounge session
  3. Wiring the DIAL HTTP handler (web.py) to the Lounge session
"""

import socket
import struct
import threading
import time
import pykka

from mopidy import core
from mopidy_dial import logger

from .lounge_session import LoungeSession
from .lounge_handler import LoungeHandler
from .lounge_subscriber import LoungeSubscriber
from .web import DialHandler

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
SSDP_ST   = "urn:dial-multiscreen-org:service:dial:1"


def _local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


class DialFrontend(pykka.ThreadingActor, core.CoreListener):

    def __init__(self, config, core):
        super().__init__()
        self.core = core
        self.config = config
        self.local_ip = _local_ip()
        self.port = config["http"]["port"]
        self.friendly_name = config.get("dial", {}).get("friendly_name", "Mopidy")
        self._running = False
        self._session = None
        self._subscriber_thread = None

    # ------------------------------------------------------------------
    # Pykka lifecycle
    # ------------------------------------------------------------------

    def on_start(self):
        self._running = True

        # Wire the on_launch callback into the shared DialHandler class
        DialHandler.on_launch = self._on_dial_launch

        # Start SSDP in background threads
        threading.Thread(target=self._run_ssdp, daemon=True).start()
        threading.Thread(target=self._run_notify, daemon=True).start()

        # Start the manual PIN pairing session
        threading.Thread(target=self._start_lounge_session, daemon=True).start()

    def on_stop(self):
        self._running = False

    # ------------------------------------------------------------------
    # DIAL launch callback (called from web.py when app POSTs)
    # ------------------------------------------------------------------

    def _on_dial_launch(self, lounge_token: str, screen_id: str):
        """
        Called when a YouTube app on the LAN taps cast and POSTs to /dial/apps/YouTube.
        Starts (or restarts) a Lounge session with the app-provided token.
        """
        logger.info(f"DIAL launch received — screenId={screen_id!r}")
        threading.Thread(
            target=self._run_session,
            args=(screen_id, lounge_token),
            daemon=True,
        ).start()

    # ------------------------------------------------------------------
    # Lounge session management
    # ------------------------------------------------------------------

    def _start_lounge_session(self):
        """
        Create a self-registered Lounge session for manual PIN pairing.
        Runs in a background thread so it doesn't block on_start().
        """
        try:
            session = LoungeSession.create(self.friendly_name)
            code = session.get_pairing_code()
            logger.info(f"YouTube pairing code: {code}")
            logger.info("Enter this code in YouTube → Settings → Watch on TV")

            # Expose session info to the DIAL handler for the status page
            DialHandler.screen_id = session.screen_id
            DialHandler.lounge_token = session.lounge_token
            DialHandler.pairing_code = code

            self._run_session(session.screen_id, session.lounge_token,
                              session=session)
        except Exception as e:
            logger.error(f"Failed to start Lounge session: {e}", exc_info=True)

    def _run_session(self, screen_id: str, lounge_token: str,
                     session: LoungeSession = None):
        """
        Bind and subscribe with the given credentials.
        If session is None, create a new one from the provided tokens
        (used for DIAL-launched sessions where the app supplies the token).
        """
        try:
            if session is None:
                session = LoungeSession(screen_id, lounge_token,
                                        self.friendly_name)
            session.bind()
            logger.info(f"Lounge session bound — SID={session.sid}")

            handler = LoungeHandler(session, self.core)
            subscriber = LoungeSubscriber(session, handler)
            result = subscriber.run_forever()
            logger.info(f"Lounge subscriber exited: {result}")

            if result == "rebind":
                # SID stale — re-bind and continue
                logger.info("Rebinding session...")
                self._run_session(screen_id, lounge_token)

        except Exception as e:
            logger.error(f"Lounge session error: {e}", exc_info=True)

    # ------------------------------------------------------------------
    # SSDP — respond to M-SEARCH from apps on the LAN
    # ------------------------------------------------------------------

    def _ssdp_response(self) -> bytes:
        location = f"http://{self.local_ip}:{self.port}/dial/ssdp/device-desc.xml"
        return (
            "HTTP/1.1 200 OK\r\n"
            f"LOCATION: {location}\r\n"
            "CACHE-CONTROL: max-age=1800\r\n"
            "CONFIGID.UPNP.ORG: 7337\r\n"
            "BOOTID.UPNP.ORG: 7337\r\n"
            f"USN: uuid:{DialHandler.device_uuid}::{SSDP_ST}\r\n"
            f"ST: {SSDP_ST}\r\n"
            "\r\n"
        ).encode()

    def _run_ssdp(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM,
                             socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except AttributeError:
            pass
        sock.bind(("", SSDP_PORT))
        mreq = struct.pack("4sL", socket.inet_aton(SSDP_ADDR),
                           socket.INADDR_ANY)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.settimeout(1.0)
        logger.info(f"SSDP listening on {SSDP_ADDR}:{SSDP_PORT}")

        while self._running:
            try:
                data, addr = sock.recvfrom(4096)
                msg = data.decode("utf-8", errors="replace")
                if "M-SEARCH" in msg and (
                    SSDP_ST in msg or "ssdp:all" in msg
                ):
                    logger.debug(f"SSDP M-SEARCH from {addr[0]}")
                    time.sleep(0.1)
                    sock.sendto(self._ssdp_response(), addr)
            except socket.timeout:
                continue
            except Exception as e:
                if self._running:
                    logger.error(f"SSDP error: {e}")
        sock.close()

    def _run_notify(self):
        """Send periodic NOTIFY alive so apps keep seeing the device."""
        location = f"http://{self.local_ip}:{self.port}/dial/ssdp/device-desc.xml"

        def packet(nts="ssdp:alive"):
            return (
                "NOTIFY * HTTP/1.1\r\n"
                f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
                "CACHE-CONTROL: max-age=1800\r\n"
                f"LOCATION: {location}\r\n"
                f"NT: {SSDP_ST}\r\n"
                f"NTS: {nts}\r\n"
                f"USN: uuid:{DialHandler.device_uuid}::{SSDP_ST}\r\n"
                "\r\n"
            ).encode()

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM,
                             socket.IPPROTO_UDP)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)

        for _ in range(3):
            sock.sendto(packet(), (SSDP_ADDR, SSDP_PORT))
            time.sleep(0.2)

        while self._running:
            time.sleep(30)
            if self._running:
                sock.sendto(packet(), (SSDP_ADDR, SSDP_PORT))

        sock.sendto(packet("ssdp:byebye"), (SSDP_ADDR, SSDP_PORT))
        sock.close()
