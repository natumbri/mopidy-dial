"""
lounge_handler.py
-----------------
Application-level event handler.
"""

from .lounge_commander import LoungeCommander
from .lounge_session import LoungeSession
from .web import DialHandler
from mopidy_dial import logger


class LoungeHandler:
    def __init__(self, session: LoungeSession, core=None):
        self.session = session
        self.core = core
        self.commander = LoungeCommander(session)

        self.current_video_id = ""
        self.current_list_id = ""
        self.current_time = "0"
        self.duration = "0"
        self.loaded_time = "0"
        self.state = "-1"       # -1=idle, 1=playing, 2=paused, 3=buffering
        self.volume = 50
        self.muted = False

    def on_event(self, event_name: str, event_args):
        logger.debug(f"Event: {event_name}  args={event_args!r}")

        if event_name == "getNowPlaying":
            self._handle_get_now_playing()
        elif event_name == "getDiscoveryDeviceId":
            self._handle_get_discovery_device_id()
        elif event_name == "getSubtitlesTrack":
            self._handle_get_subtitles_track()
        elif event_name == "getVolume":
            self._handle_get_volume()
        elif event_name == "getPartyGamesMode":
            self._handle_get_party_games_mode()
        elif event_name == "setPlaylist":
            self._handle_set_playlist(event_args)
        elif event_name == "updatePlaylist":
            self._handle_update_playlist(event_args)
        elif event_name == "addVideo":
            self._handle_add_video(event_args)
        elif event_name == "play":
            self._handle_play()
        elif event_name == "pause":
            self._handle_pause()
        elif event_name == "stopVideo":
            self._handle_stop()
        elif event_name == "seekTo":
            self._handle_seek(event_args)
        elif event_name == "setVolume":
            self._handle_set_volume(event_args)
        elif event_name == "next":
            self._handle_next()
        elif event_name == "previous":
            self._handle_previous()
        elif event_name == "setPlaybackSpeed":
            pass  # no response required
        elif event_name == "remoteConnected":
            if isinstance(event_args, dict):
                DialHandler.state["connected"] = True
                DialHandler.state["remote_name"] = event_args.get("name", "Unknown")
                DialHandler.state["remote_app"] = event_args.get("app", "")

        elif event_name == "remoteDisconnected":
            DialHandler.state["connected"] = False
            DialHandler.state["remote_name"] = None
            DialHandler.state["remote_app"] = None

        elif event_name == "loungeScreenDisconnected":
            DialHandler.state["connected"] = False
            reason = event_args.get("reason", "?") if isinstance(event_args, dict) else event_args
            logger.warning(f"  *** SCREEN DISCONNECTED: reason={reason!r} ***")

        elif event_name in ("noop", "loungeStatus", "S", "c",
                            "onAutoplayModeChanged", "onPlaylistModeChanged",
                            "playlistModified"):
            pass
        else:
            logger.warning(f"  (unhandled: {event_name!r})")

    # ---------------------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------------------

    def _now_playing_params(self):
        return {
            "videoId": self.current_video_id,
            "listId": self.current_list_id,
            "state": self.state,
            "currentTime": self.current_time,
            "duration": self.duration,
            "loadedTime": self.loaded_time,
            "seekableStartTime": "0",
            "seekableEndTime": self.duration,
            "cpn": "",
        }

    def _state_change_params(self):
        return {
            "state": self.state,
            "currentTime": self.current_time,
            "duration": self.duration,
            "loadedTime": self.loaded_time,
            "seekableStartTime": "0",
            "seekableEndTime": self.duration,
            "cpn": "",
        }

    # ---------------------------------------------------------------------------
    # Mandatory handshake responses (app disconnects if these are missing)
    # ---------------------------------------------------------------------------

    def _handle_get_now_playing(self):
        # Must send nowPlaying + onStateChange together
        self.commander.send("nowPlaying", self._now_playing_params())
        self.commander.send("onStateChange", self._state_change_params())

    def _handle_get_discovery_device_id(self):
        self.commander.send("onDiscoveryDeviceIdRetrieved", {
            "discoveryDeviceId": self.session.screen_id,
        })

    def _handle_get_subtitles_track(self):
        # Report no active subtitles track
        self.commander.send("onSubtitlesTrackChanged", {
            "videoId": self.current_video_id,
            "trackId": "",
            "trackName": "",
        })

    def _handle_get_volume(self):
        DialHandler.state["volume"] = self.volume
        DialHandler.state["muted"] = self.muted
        self.commander.send("onVolumeChanged", {
            "volume": str(self.volume),
            "muted": str(self.muted).lower(),
        })

    def _handle_get_party_games_mode(self):
        # Report party games not active
        self.commander.send("onPartyGamesModeChanged", {
            "partyGamesMode": "PARTY_GAMES_MODE_NONE",
        })

    # ---------------------------------------------------------------------------
    # Playback control
    # ---------------------------------------------------------------------------

    def _handle_set_playlist(self, args: dict):
        if not isinstance(args, dict):
            return
        self.current_video_id = args.get("videoId", "")
        self.current_list_id = args.get("listId", "")
        self.current_time = args.get("currentTime", "0")
        self.duration = "0"
        self.loaded_time = "0"

        # Respect the sender's requested playback state.
        # 'PAUSED' means queued but not started; anything else → play immediately.
        sender_state = args.get("playbackState", "PLAYING")
        initial_state = "2" if sender_state == "PAUSED" else "1"

        logger.info(f"  ▶ setPlaylist: videoId={self.current_video_id!r} "
              f"listId={self.current_list_id!r} "
              f"sender_state={sender_state!r}")

        DialHandler.state["video_id"] = self.current_video_id
        DialHandler.state["list_id"] = self.current_list_id
        DialHandler.state["playback_state"] = "buffering"

        # Report buffering first, then the initial state
        self.state = "3"
        self.commander.send("nowPlaying", self._now_playing_params())
        self.commander.send("onStateChange", self._state_change_params())
        self.state = initial_state
        DialHandler.state["playback_state"] = "paused" if initial_state == "2" else "playing"
        self.commander.send("onStateChange", self._state_change_params())

        # TODO: hand off to Mopidy here
        # e.g. self.mopidy.play_youtube(self.current_video_id, paused=(initial_state == "2"))
        self.core.tracklist.add(uris=[f"yt:video:{self.current_video_id}"])

    def _handle_update_playlist(self, args: dict):
        if not isinstance(args, dict):
            return
        logger.info(args["mdxQueueOperations"])
        try:
            videoId = args["mdxQueueOperations"][0]["videos"][0]["externalVideoId"]
            logger.info(f"  + addVideo: videoId={args.get('videoId', '')!r}")
            self.core.tracklist.add(uris=[f"yt:video:{videoId}"])        
        except Exception as e:
            logger.info(f"Could not update playlist: {e}")

    def _handle_add_video(self, args: dict):
        if not isinstance(args, dict):
            return
        videoId = args.get('videoId')
        if videoId:
            logger.info(f"  + addVideo: videoId={args.get('videoId', '')!r}")
            # TODO: add to Mopidy queue
            self.core.tracklist.add(uris=[f"yt:video:{video_id}"])


    def _handle_play(self):
        self.state = "1"
        DialHandler.state["playback_state"] = "playing"
        self.commander.send("onStateChange", self._state_change_params())
        # TODO: resume Mopidy

    def _handle_pause(self):
        self.state = "2"
        DialHandler.state["playback_state"] = "paused"
        self.commander.send("onStateChange", self._state_change_params())
        # TODO: pause Mopidy

    def _handle_stop(self):
        self.state = "-1"
        self.current_video_id = ""
        self.current_time = "0"
        self.duration = "0"
        DialHandler.state.update({"playback_state": "idle", "video_id": "", "current_time": 0.0})
        self.commander.send("nowPlaying", self._now_playing_params())
        self.commander.send("onStateChange", self._state_change_params())
        # TODO: stop Mopidy

    def _handle_seek(self, args: dict):
        if not isinstance(args, dict):
            return
        self.current_time = args.get("newTime", self.current_time)
        self.commander.send("onStateChange", self._state_change_params())
        # TODO: seek Mopidy

    def _handle_set_volume(self, args: dict):
        if not isinstance(args, dict):
            return
        if "volume" in args:
            self.volume = int(args["volume"])
        elif "delta" in args:
            self.volume = max(0, min(100, self.volume + int(args["delta"])))
        self.commander.send("onVolumeChanged", {
            "volume": str(self.volume),
            "muted": str(self.muted).lower(),
        })
        # TODO: set Mopidy volume

    def _handle_next(self):
        print("  ⏭ next")
        # TODO: next track in Mopidy

    def _handle_previous(self):
        print("  ⏮ previous")
        # TODO: previous track in Mopidy
