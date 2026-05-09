import tornado.web
import urllib.parse

DEVICE_XML_TEMPLATE = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0" xmlns:sec="http://samsung.com">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <device>
    <deviceType>urn:schemas-upnp-org:device:TVDevice:1</deviceType>
    <manufacturer>Mopidy</manufacturer>
    <modelName>Mopidy YouTube Cast Receiver</modelName>
    <friendlyName>{friendly_name}</friendlyName>
    <UDN>uuid:audio-streamer-12345</UDN>
    <serviceList>
      <service>
        <serviceType>urn:dial-multiscreen-org:service:dial:1</serviceType>
        <serviceId>urn:dial-multiscreen-org:serviceId:dial</serviceId>
        <SCPDURL>/dial/YouTube</SCPDURL>
        <controlURL>/dial/YouTube</controlURL>
        <eventSubURL></eventSubURL>
      </service>
    </serviceList>
  </device>
</root>"""

class DialHandler(tornado.web.RequestHandler):
    def initialize(self, core, config):
        self.core = core
        self.config = config

    def get(self, path):
        # Standard DIAL path for app status: /dial/apps/YouTube
        self.set_header("Access-Control-Allow-Origin", "*")
        self.set_header("Access-Control-Expose-Headers", "Application-URL, Location")
        # Some YouTube clients require the Origin to be explicitly handled
        if self.request.headers.get("Origin"):
            self.set_header("Access-Control-Allow-Origin", self.request.headers.get("Origin"))

        if path == "apps/YouTube":
            self.set_header("Content-Type", "application/xml")
            # YT Music specifically looks for the 'stopped' or 'running' state
            # dialVer="2.1" is recommended for modern app compatibility
            xml_status = (
                '<?xml version="1.0" encoding="UTF-8"?>\r\n'
                '<service xmlns="urn:dial-multiscreen-org:schemas:dial" dialVer="2.1">\r\n'
                '  <name>YouTube</name>\r\n'
                '  <options allowStop="true"/>\r\n'
                '  <state>stopped</state>\r\n'
                '</service>'
            )
            self.write(xml_status)
        elif path == "device.xml":
            self.set_header("Content-Type", "application/xml")
            # Inject the friendly name from your mopidy.conf
            xml_output = DEVICE_XML_TEMPLATE.format(
                friendly_name=self.config.get('dial', {}).get('hostname', 'Mopidy')
            )
            self.write(xml_output)
        else:
            self.set_status(404)

    def post(self, path):
        self.set_header("Access-Control-Allow-Origin", "*")
        self.set_header("Access-Control-Expose-Headers", "Application-URL, Location")
        # Some YouTube clients require the Origin to be explicitly handled
        if self.request.headers.get("Origin"):
            self.set_header("Access-Control-Allow-Origin", self.request.headers.get("Origin"))

        if path == "apps/YouTube":
            data = self.request.body.decode('utf-8')
            params = urllib.parse.parse_qs(data)
            video_id = params.get('v', [None])[0]

            if video_id:
                uri = f"youtube:video:{video_id}"
                self.core.tracklist.clear()
                self.core.tracklist.add(uris=[uri])
                self.core.playback.play()
                self.set_status(201)
                self.set_header("Location", f"http://{self.request.host}/dial/apps/YouTube/run")
            else:
                self.set_status(400)
        self.finish()
