import logging
import pathlib
from importlib.metadata import version

from mopidy import config, ext

__version__ = version("mopidy-dial")

# TODO: If you need to log, use loggers named after the current Python module
logger = logging.getLogger(__name__)


class Extension(ext.Extension):
    dist_name = "mopidy-dial"
    ext_name = "dial"
    version = __version__

    def get_default_config(self):
        return config.read(pathlib.Path(__file__).parent / "ext.conf")

    def get_config_schema(self):
        schema = super().get_config_schema()
        # TODO: Comment in and edit, or remove entirely
        #schema["username"] = config.String()
        #schema["password"] = config.Secret()
        schema["friendly_name"] = config.String()
        return schema


    def setup(self, registry):
        from .frontend import DialFrontend
        from .web import DialHandler

        registry.add("frontend", DialFrontend)
        
        # Mopidy expects a list of (regexp, handler_class) tuples
        registry.add("http:app", {
            "name": self.ext_name, # "dial"
            "factory": lambda config, core: [
                (r"/(.*)", DialHandler, {"config": config, "core": core}),
            ],
        })
