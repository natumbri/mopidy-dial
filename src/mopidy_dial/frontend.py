import socket
import struct
import threading
import logging
import pykka
from mopidy import core

logger = logging.getLogger(__name__)

class DialFrontend(pykka.ThreadingActor, core.CoreListener):
    def __init__(self, config, core):
        super().__init__()
        self.core = core
        self.active = True
        # Pull config from mopidy.conf
        self.ip = config['dial']['hostname']
        self.port = 8080  # Default Mopidy HTTP port
        self.mac = config['dial']['mac_address']

    def on_start(self):
        self.ssdp_thread = threading.Thread(target=self._run_ssdp)
        self.ssdp_thread.daemon = True
        self.ssdp_thread.start()

    def _run_ssdp(self):
        MULTICAST_GROUP = "239.255.255.250"
        RESPONSE = (
            "HTTP/1.1 200 OK\r\n"
            "CACHE-CONTROL: max-age=1800\r\n"
            f"LOCATION: http://{self.ip}:{self.port}/dial/device.xml\r\n"
            "ST: urn:dial-multiscreen-org:service:dial:1\r\n"
            "USN: uuid:audio-streamer-12345::urn:dial-multiscreen-org:service:dial:1\r\n"
            f"Application-URL: http://{self.ip}:{self.port}/dial/apps/\r\n"
            "SERVER: Mopidy/1.0 UPnP/1.0 yt-dial-receiver/0.1\r\n"
            "EXT:\r\n\r\n"
        ).encode("utf-8")

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('', 1900))
        
        mreq = struct.pack("4sl", socket.inet_aton(MULTICAST_GROUP), socket.INADDR_ANY)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

        while self.active:
            data, addr = sock.recvfrom(1024)
            if b"M-SEARCH" in data and b"dial" in data.lower():
                sock.sendto(RESPONSE, addr)
