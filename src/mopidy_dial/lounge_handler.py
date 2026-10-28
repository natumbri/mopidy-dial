"""
lounge_handler.py
-----------------
Handles all YouTube Lounge events and maps them to Mopidy core API calls.

YouTube → Mopidy mapping:
  setPlaylist        → tracklist.clear + tracklist.add + playback.play
  addVideo           → tracklist.add
  removeVideo        → tracklist.remove
  updatePlaylist     → tracklist.add (from mdxQueueOperations)
  play               → playback.resume
  pause              → playback.pause
  stopVideo          → playback.stop
  seekTo             → playback.seek  (ms conversion)
  next               → playback.next
  previous           → playback.previous
  setVolume          → mixer.set_volume / mixer.set_mute
  getVolume          → mixer.get_volume + report onVolumeChanged
  setPlaybackSpeed   → acknowledged only (Mopidy has no speed control)

Mopidy → YouTube (via CoreListener events in frontend.py):
  track_playback_started  → nowPlaying + onStateChange(playing)
  track_playback_paused   → onStateChange(paused)
  track_playback_resumed  → onStateChange(playing)
  track_playback_ended    → nowPlaying(idle) + onStateChange(idle)
  seeked                  → onStateChange with updated position
  volume_changed          → onVolumeChanged
  mute_changed            → onVolumeChanged

URI scheme used for mopidy-youtube: yt:video:<video_id>
"""

import re
import secrets
import threading
import time

from .lounge_commander import LoungeCommander
from .lounge_session import LoungeSession
from .web import DialHandler
from mopidy_dial import logger


def _yt_uri(video_id: str) -> str:
    return f"yt:video:{video_id}"


_VIDEO_URI_RE = re.compile(r"^(?:yt|youtube):video[:/]([\w-]+)$")


def _video_id(track) -> str:
    """Video id of a Mopidy track (mopidy-youtube uses youtube:video:<id>)."""
    m = _VIDEO_URI_RE.match(track.uri or "")
    return m.group(1) if m else (track.comment or "")


def _flag(value: bool) -> str:
    return "true" if value else "false"


def _ms_to_sec(ms) -> str:
    """Convert milliseconds (int) to seconds string for Lounge API."""
    try:
        return str(int(ms) // 1000)
    except (TypeError, ValueError):
        return "0"


def _sec_to_ms(sec) -> int:
    """Convert seconds (str/float) to milliseconds (int) for Mopidy API."""
    try:
        return int(float(sec) * 1000)
    except (TypeError, ValueError):
        return 0


class LoungeHandler:
    def __init__(self, session: LoungeSession, core=None):
        self.session = session
        self.core = core
        self.commander = LoungeCommander(session)

        # Local state mirrors what we've told the Lounge
        self.current_video_id = ""
        self.current_list_id = ""
        self.current_time = "0"     # seconds, as string
        self.duration = "0"         # seconds, as string
        self.loaded_time = "0"
        self.state = "-1"           # -1=idle, 1=playing, 2=paused, 3=buffering
        self.volume = 50
        self.muted = False

        # Serialises queue edits, which can take a while (each added video
        # is looked up). _queue_gen invalidates a fill when a newer one starts.
        self._queue_lock = threading.Lock()
        self._queue_gen = 0

        # True while a remote (phone) is connected to this session
        self.connected = False

        # Context echoed back to the remote, as a real TV does
        self.ctt = ""
        self.current_index = "0"
        self.cpn = ""

        # Periodic position reports while playing (see _start_ticker)
        self._tick_gen = 0

    # -------------------------------------------------------------------------
    # Event dispatcher
    # -------------------------------------------------------------------------

    def on_event(self, event_name: str, event_args):
        logger.debug(f"Lounge event: {event_name}  args={str(event_args)[:300]}")

        # --- Mandatory handshake queries ---
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

        # --- Playback commands ---
        elif event_name == "setPlaylist":
            self._handle_set_playlist(event_args)
        elif event_name == "addVideo":
            self._handle_add_video(event_args)
        elif event_name == "removeVideo":
            self._handle_remove_video(event_args)
        elif event_name == "updatePlaylist":
            self._handle_update_playlist(event_args)
        elif event_name == "play":
            self._handle_play()
        elif event_name == "pause":
            self._handle_pause()
        elif event_name == "stopVideo":
            self._handle_stop()
        elif event_name == "seekTo":
            self._handle_seek(event_args)
        elif event_name == "next":
            self._handle_next()
        elif event_name == "previous":
            self._handle_previous()

        # --- Volume ---
        elif event_name == "setVolume":
            self._handle_set_volume(event_args)

        # --- Queue/playlist mode ---
        elif event_name == "setLoop":
            self._handle_set_loop(event_args)
        elif event_name == "setShuffle":
            self._handle_set_shuffle(event_args)
        elif event_name == "setPlaybackSpeed":
            # Mopidy has no playback speed control; just acknowledge
            pass

        # --- Session events ---
        elif event_name == "remoteConnected":
            self._handle_remote_connected(event_args)
        elif event_name == "remoteDisconnected":
            self._handle_remote_disconnected(event_args)
        elif event_name == "loungeScreenDisconnected":
            self.connected = False
            DialHandler.state["connected"] = False
            reason = (event_args.get("reason", "?")
                      if isinstance(event_args, dict) else event_args)
            logger.warning(f"Screen disconnected: reason={reason!r}")

        # --- Silently acknowledged ---
        elif event_name in (
            "noop", "loungeStatus", "S", "c",
            "onAutoplayModeChanged", "onPlaylistModeChanged",
            "playlistModified", "autoplayUpNext", "setSubtitlesTrack",
        ):
            pass

        else:
            logger.warning(f"Unhandled Lounge event: {event_name!r}")

    # -------------------------------------------------------------------------
    # Lounge response helpers
    # -------------------------------------------------------------------------

    def _now_playing_params(self) -> dict:
        return {
            "videoId":          self.current_video_id,
            "listId":           self.current_list_id,
            "state":            self.state,
            "currentTime":      self.current_time,
            "duration":         self.duration,
            "loadedTime":       self.loaded_time,
            "seekableStartTime":"0",
            "seekableEndTime":  self.duration,
            "cpn":              self.cpn,
            "ctt":              self.ctt,
            "currentIndex":     self.current_index,
        }

    def _state_change_params(self) -> dict:
        return {
            "state":            self.state,
            "currentTime":      self.current_time,
            "duration":         self.duration,
            "loadedTime":       self.loaded_time,
            "seekableStartTime":"0",
            "seekableEndTime":  self.duration,
            "cpn":              self.cpn,
        }

    TICK_SECONDS = 5

    def _start_ticker(self):
        """
        The YouTube remote only extrapolates the playback position from the
        last report, so keep sending the real position while playing.
        """
        self._tick_gen += 1
        gen = self._tick_gen

        def run():
            while gen == self._tick_gen and self.state == "1":
                time.sleep(self.TICK_SECONDS)
                if gen != self._tick_gen or self.state != "1":
                    return
                self._sync_time_from_mopidy()
                self._report_state()

        threading.Thread(target=run, daemon=True).start()

    def _stop_ticker(self):
        self._tick_gen += 1

    def _report_state(self, also_now_playing: bool = False):
        """Send current state to the Lounge (and optionally nowPlaying)."""
        if also_now_playing:
            self.commander.send("nowPlaying", self._now_playing_params())
        self.commander.send("onStateChange", self._state_change_params())

    def _update_web_state(self):
        DialHandler.state.update({
            "video_id":       self.current_video_id,
            "list_id":        self.current_list_id,
            "current_time":   float(self.current_time),
            "duration":       float(self.duration),
        })

    # -------------------------------------------------------------------------
    # Mandatory handshake handlers
    # -------------------------------------------------------------------------

    def _handle_get_now_playing(self):
        # Sync current time from Mopidy before reporting
        self._sync_time_from_mopidy()
        self._report_state(also_now_playing=True)

    def _handle_get_discovery_device_id(self):
        self.commander.send("onDiscoveryDeviceIdRetrieved", {
            "discoveryDeviceId": self.session.screen_id,
        })

    def _handle_get_subtitles_track(self):
        self.commander.send("onSubtitlesTrackChanged", {
            "videoId":   self.current_video_id,
            "trackId":   "",
            "trackName": "",
        })

    def _handle_get_volume(self):
        self._sync_volume_from_mopidy()
        self.commander.send("onVolumeChanged", {
            "volume": str(self.volume),
            "muted":  str(self.muted).lower(),
        })

    def _handle_get_party_games_mode(self):
        self.commander.send("onPartyGamesModeChanged", {
            "partyGamesMode": "PARTY_GAMES_MODE_NONE",
        })

    # -------------------------------------------------------------------------
    # Playback handlers
    # -------------------------------------------------------------------------

    def _handle_set_playlist(self, args: dict):
        if not isinstance(args, dict):
            return

        video_id   = args.get("videoId", "")
        list_id    = args.get("listId", "")
        start_time = args.get("currentTime", "0")
        # PAUSED = user queued but didn't tap play; anything else = play now
        sender_state = args.get("playbackState", "PLAYING")
        start_paused = (sender_state == "PAUSED")

        logger.info(f"setPlaylist: videoId={video_id!r} "
                    f"listId={list_id!r} paused={start_paused}")

        self.current_video_id = video_id
        self.current_list_id  = list_id
        self.ctt              = args.get("ctt", "")
        self.current_index    = str(args.get("currentIndex", "0"))
        self.cpn              = secrets.token_urlsafe(12)[:16]
        self.current_time     = start_time
        self.duration         = "0"
        self.loaded_time      = "0"

        # Tell Lounge we're buffering
        self.state = "3"
        DialHandler.state["playback_state"] = "buffering"
        self._report_state(also_now_playing=True)
        self._update_web_state()

        if self.core:
            try:
                ids, index = self._parse_queue(args)
                tlid = self._load_queue(ids, index)
                if tlid is not None:
                    # Play then immediately pause so mopidy-youtube resolves
                    # the stream URI, if the sender didn't ask for playback
                    self._pending_pause = start_paused
                    self.core.playback.play(tlid=tlid).get()
                    self._watch_for_start(video_id)

                    if start_time and float(start_time) > 0:
                        self.core.playback.seek(
                            time_position=_sec_to_ms(start_time)
                        ).get()
                else:
                    logger.error(f"No playable tracks for {video_id!r}")
                    self._report_idle()

            except Exception as e:
                logger.error(f"setPlaylist error: {e}", exc_info=True)
                self._report_idle()
        else:
            # No core — simulate playing for standalone testing
            self.state = "2" if start_paused else "1"
            DialHandler.state["playback_state"] = "paused" if start_paused else "playing"
            self._report_state()

    START_TIMEOUT = 20

    def _watch_for_start(self, video_id: str):
        """
        If Mopidy never starts the track (e.g. mopidy-youtube can't fetch the
        audio), the remote would sit on "buffering" forever. Report idle.
        """
        def check():
            if self.state != "3" or self.current_video_id != video_id:
                return  # started, or superseded by another request
            try:
                state = self.core.playback.get_state().get()
            except Exception:
                state = None
            if state != "playing":
                logger.warning(f"Track {video_id!r} did not start playing "
                               f"(mopidy state={state!r}) — reporting idle")
                self._report_idle()

        timer = threading.Timer(self.START_TIMEOUT, check)
        timer.daemon = True
        timer.start()

    def _handle_add_video(self, args: dict):
        if not isinstance(args, dict):
            return
        video_id = args.get("videoId", "")
        if not video_id:
            return
        logger.info(f"addVideo: videoId={video_id!r}")
        if self.core:
            try:
                self.core.tracklist.add(uris=[_yt_uri(video_id)]).get()
            except Exception as e:
                logger.error(f"addVideo error: {e}", exc_info=True)

    def _handle_remove_video(self, args: dict):
        if not isinstance(args, dict):
            return
        video_id = args.get("videoId", "")
        if not video_id:
            return
        logger.info(f"removeVideo: videoId={video_id!r}")
        if self.core:
            try:
                # Find all tracklist entries for this URI and remove them
                self.core.tracklist.remove(
                    criteria={"uri": [_yt_uri(video_id)]}
                ).get()
            except Exception as e:
                logger.error(f"removeVideo error: {e}", exc_info=True)

    # -------------------------------------------------------------------------
    # Queue handling
    #
    # setPlaylist and updatePlaylist both carry the whole queue as a
    # comma-separated `videoIds`, plus `currentIndex`. "Add to queue" and
    # "play next" arrive as updatePlaylist with the new list.
    # -------------------------------------------------------------------------

    @staticmethod
    def _parse_queue(args: dict) -> tuple[list[str], int]:
        """Return (video ids, index of the current one) from a message."""
        ids = [v for v in str(args.get("videoIds", "")).split(",") if v]
        current = args.get("videoId", "")
        if current and current not in ids:
            ids = [current]
        if not ids:
            return [], 0
        try:
            index = int(args.get("currentIndex", ""))
        except (TypeError, ValueError):
            index = -1
        if not 0 <= index < len(ids) or (current and ids[index] != current):
            index = ids.index(current) if current in ids else 0
        return ids, index

    def _tracklist_ids(self) -> tuple[list, list[str]]:
        tl_tracks = self.core.tracklist.get_tl_tracks().get()
        return tl_tracks, [_video_id(t.track) for t in tl_tracks]

    def _load_queue(self, ids: list[str], index: int):
        """
        Make the Mopidy tracklist match `ids` and return the tlid of
        ids[index]. The current video is added first so playback can begin
        straight away; the rest is filled in on a background thread.
        """
        if not ids:
            return None
        with self._queue_lock:
            self._queue_gen += 1
            tl_tracks, have = self._tracklist_ids()
            if have == ids:
                return tl_tracks[index].tlid  # same queue, just jump

            self.core.tracklist.clear().get()
            added = self.core.tracklist.add(
                uris=[_yt_uri(ids[index])]).get()
            if not added:
                return None
            tlid = added[0].tlid
            gen = self._queue_gen

        if len(ids) > 1:
            threading.Thread(
                target=self._fill_queue, args=(ids, index, gen),
                daemon=True,
            ).start()
        return tlid

    def _fill_queue(self, ids: list[str], index: int, gen: int):
        """Add the videos before and after the current one."""
        try:
            with self._queue_lock:
                if gen != self._queue_gen:
                    return
                before = [_yt_uri(v) for v in ids[:index]]
                after = [_yt_uri(v) for v in ids[index + 1:]]
                if before:
                    self.core.tracklist.add(uris=before, at_position=0).get()
                if after:
                    self.core.tracklist.add(uris=after).get()
            logger.info(f"Queue loaded: {len(ids)} video(s)")
            self._refresh_queue_position()
        except Exception as e:
            logger.error(f"Filling queue failed: {e}", exc_info=True)

    def _handle_update_playlist(self, args: dict):
        """Queue edit from the remote: add, remove or reorder videos."""
        if not isinstance(args, dict) or not self.core:
            return
        ids, _ = self._parse_queue(args)
        if not ids:
            return
        logger.info(f"updatePlaylist: {len(ids)} video(s)")
        self.current_list_id = args.get("listId", self.current_list_id)
        self.ctt = args.get("ctt", self.ctt)
        threading.Thread(target=self._sync_queue, args=(ids,),
                         daemon=True).start()

    def _sync_queue(self, ids: list[str]):
        """Edit the tracklist, minimally, until it matches `ids`."""
        try:
            with self._queue_lock:
                self._queue_gen += 1  # supersede any fill in progress
                tl_tracks, have = self._tracklist_ids()

                # Remove tracks that are no longer queued
                wanted = {}
                for v in ids:
                    wanted[v] = wanted.get(v, 0) + 1
                drop = []
                for t, v in zip(tl_tracks, have):
                    if wanted.get(v, 0) > 0:
                        wanted[v] -= 1
                    else:
                        drop.append(t.tlid)
                if drop:
                    self.core.tracklist.remove(criteria={"tlid": drop}).get()

                # Then add / move into place, front to back
                for i, v in enumerate(ids):
                    _, now = self._tracklist_ids()
                    if i < len(now) and now[i] == v:
                        continue
                    if v in now[i + 1:]:
                        j = i + 1 + now[i + 1:].index(v)
                        self.core.tracklist.move(
                            start=j, end=j + 1, to_position=i).get()
                    else:
                        self.core.tracklist.add(
                            uris=[_yt_uri(v)], at_position=i).get()
            self._refresh_queue_position()
        except Exception as e:
            logger.error(f"updatePlaylist error: {e}", exc_info=True)

    def _refresh_queue_position(self, report: bool = True):
        """Tell the remote where we are in the queue and what's around it."""
        try:
            current = self.core.playback.get_current_tl_track().get()
            length = self.core.tracklist.get_length().get()
            index = (self.core.tracklist.index(tlid=current.tlid).get()
                     if current else None)
        except Exception:
            return
        if index is not None:
            self.current_index = str(index)
        has_prev = index is not None and index > 0
        has_next = index is not None and index < length - 1
        self.commander.send("onHasPreviousNextChanged", {
            "hasPrevious": _flag(has_prev),
            "hasNext":     _flag(has_next),
        })
        if report and self.state in ("1", "2"):
            self._report_state(also_now_playing=True)

    def _handle_play(self):
        logger.info("play")
        if self.core:
            try:
                self.core.playback.resume().get()
            except Exception as e:
                logger.error(f"play error: {e}", exc_info=True)
        else:
            self.state = "1"
            DialHandler.state["playback_state"] = "playing"
            self._report_state()

    def _handle_pause(self):
        logger.info("pause")
        if self.core:
            try:
                self.core.playback.pause().get()
            except Exception as e:
                logger.error(f"pause error: {e}", exc_info=True)
        else:
            self.state = "2"
            DialHandler.state["playback_state"] = "paused"
            self._report_state()

    def _handle_stop(self):
        logger.info("stopVideo")
        if self.core:
            try:
                self.core.playback.stop().get()
                self.core.tracklist.clear().get()
            except Exception as e:
                logger.error(f"stop error: {e}", exc_info=True)
        else:
            self._report_idle()

    def _handle_seek(self, args: dict):
        if not isinstance(args, dict):
            return
        new_time_sec = args.get("newTime", self.current_time)
        logger.info(f"seekTo: {new_time_sec}s")
        self.current_time = str(new_time_sec)
        DialHandler.state["current_time"] = float(new_time_sec)

        if self.core:
            try:
                self.core.playback.seek(
                    time_position=_sec_to_ms(new_time_sec)
                ).get()
                # onStateChange will be sent by the seeked CoreListener event
            except Exception as e:
                logger.error(f"seek error: {e}", exc_info=True)
        else:
            self._report_state()

    def _handle_next(self):
        logger.info("next")
        if self.core:
            try:
                self.core.playback.next().get()
            except Exception as e:
                logger.error(f"next error: {e}", exc_info=True)

    def _handle_previous(self):
        logger.info("previous")
        if self.core:
            try:
                self.core.playback.previous().get()
            except Exception as e:
                logger.error(f"previous error: {e}", exc_info=True)

    # -------------------------------------------------------------------------
    # Volume handlers
    # -------------------------------------------------------------------------

    def _handle_set_volume(self, args: dict):
        if not isinstance(args, dict):
            return

        if "volume" in args:
            self.volume = max(0, min(100, int(args["volume"])))
        elif "delta" in args:
            self.volume = max(0, min(100, self.volume + int(args["delta"])))

        if "muted" in args:
            self.muted = str(args["muted"]).lower() == "true"

        logger.info(f"setVolume: volume={self.volume} muted={self.muted}")

        if self.core:
            try:
                self.core.mixer.set_volume(volume=self.volume).get()
                self.core.mixer.set_mute(mute=self.muted).get()
            except Exception as e:
                logger.error(f"setVolume error: {e}", exc_info=True)

        DialHandler.state["volume"] = self.volume
        DialHandler.state["muted"]  = self.muted
        self.commander.send("onVolumeChanged", {
            "volume": str(self.volume),
            "muted":  str(self.muted).lower(),
        })

    # -------------------------------------------------------------------------
    # Queue mode handlers
    # -------------------------------------------------------------------------

    def _handle_set_loop(self, args: dict):
        if not isinstance(args, dict):
            return
        enabled = str(args.get("loopEnabled", "false")).lower() == "true"
        logger.info(f"setLoop: enabled={enabled}")
        if self.core:
            try:
                self.core.tracklist.set_repeat(value=enabled).get()
            except Exception as e:
                logger.error(f"setLoop error: {e}", exc_info=True)

    def _handle_set_shuffle(self, args: dict):
        if not isinstance(args, dict):
            return
        enabled = str(args.get("shuffleEnabled", "false")).lower() == "true"
        logger.info(f"setShuffle: enabled={enabled}")
        if self.core:
            try:
                self.core.tracklist.set_random(value=enabled).get()
            except Exception as e:
                logger.error(f"setShuffle error: {e}", exc_info=True)

    # -------------------------------------------------------------------------
    # Session event handlers
    # -------------------------------------------------------------------------

    def _handle_remote_connected(self, args: dict):
        if isinstance(args, dict):
            self.connected = True
            DialHandler.state["connected"]   = True
            DialHandler.state["remote_name"] = args.get("name", "Unknown")
            DialHandler.state["remote_app"]  = args.get("app", "")
            logger.info(f"Remote connected: {args.get('name')!r} "
                        f"({args.get('app')!r})")

    def _handle_remote_disconnected(self, args: dict):
        self.connected = False
        DialHandler.state["connected"]   = False
        DialHandler.state["remote_name"] = None
        DialHandler.state["remote_app"]  = None
        # Reset DIAL state so Music app knows it can connect again
        logger.info("Remote disconnected")

    # -------------------------------------------------------------------------
    # CoreListener callbacks (called from DialFrontend)
    # These are the Mopidy → YouTube direction.
    # -------------------------------------------------------------------------

    def on_track_playback_started(self, tl_track):
        """Mopidy started playing a track — report playing state to Lounge."""
        track = tl_track.track
        uri   = track.uri  # e.g. yt:video:ZdC8waf4fJU

        # Extract video_id from URI
        # mopidy-youtube rewrites yt:video:<id> to youtube:video:<id>
        self.current_video_id = track.comment or uri.rsplit(":", 1)[-1]

        duration_ms = track.length or 0
        self.duration     = _ms_to_sec(duration_ms)
        self.loaded_time  = self.duration
        self.current_time = "0"

        if getattr(self, "_pending_pause", False):
            self._pending_pause = False
            self.state = "2"
            DialHandler.state["playback_state"] = "paused"
            if self.core:
                try:
                    self.core.playback.pause().get()
                except Exception:
                    pass
        else:
            self.state = "1"
            DialHandler.state["playback_state"] = "playing"

        DialHandler.state["video_id"]  = self.current_video_id
        DialHandler.state["duration"]  = float(self.duration)
        self._report_state(also_now_playing=True)
        if self.state == "1":
            self._start_ticker()
        self._refresh_queue_position(report=False)

    def on_track_playback_paused(self, tl_track, time_position):
        """Mopidy paused — report paused state to Lounge."""
        self._stop_ticker()
        self.state        = "2"
        self.current_time = _ms_to_sec(time_position)
        DialHandler.state["playback_state"] = "paused"
        DialHandler.state["current_time"]   = float(self.current_time)
        self._report_state()

    def on_track_playback_resumed(self, tl_track, time_position):
        """Mopidy resumed — report playing state to Lounge."""
        self.state        = "1"
        self.current_time = _ms_to_sec(time_position)
        DialHandler.state["playback_state"] = "playing"
        DialHandler.state["current_time"]   = float(self.current_time)
        self._report_state()
        self._start_ticker()

    def on_track_playback_ended(self, tl_track, time_position):
        """Mopidy track ended — if nothing next, report idle."""
        # Mopidy will auto-advance if there's a next track;
        # on_track_playback_started will fire then.
        # We only need to act if this was the last track.
        if self.core:
            try:
                # Playback is already stopped here, so get_next_tlid() has no
                # current track to work from and returns the head of the list.
                # Ask for the successor of the track that ended instead.
                next_tl = self.core.tracklist.next_track(tl_track).get()
                logger.debug(f"next track after end: {next_tl!r}")
                if next_tl is None:
                    self._report_idle()
            except Exception as e:
                logger.warning(f"next_track lookup failed: {e}", exc_info=True)
                self._report_idle()
        else:
            self._report_idle()

    def on_seeked(self, time_position):
        """Mopidy seeked — report new position to Lounge."""
        self.current_time = _ms_to_sec(time_position)
        DialHandler.state["current_time"] = float(self.current_time)
        self._report_state()

    def on_volume_changed(self, volume):
        """Mopidy volume changed externally — report to Lounge."""
        self.volume = volume
        DialHandler.state["volume"] = volume
        self.commander.send("onVolumeChanged", {
            "volume": str(volume),
            "muted":  str(self.muted).lower(),
        })

    def on_mute_changed(self, mute):
        """Mopidy mute changed externally — report to Lounge."""
        self.muted = mute
        DialHandler.state["muted"] = mute
        self.commander.send("onVolumeChanged", {
            "volume": str(self.volume),
            "muted":  str(mute).lower(),
        })

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _report_idle(self):
        self._stop_ticker()
        self.state            = "-1"
        self.current_video_id = ""
        self.current_time     = "0"
        self.duration         = "0"
        self.loaded_time      = "0"
        DialHandler.state.update({
            "playback_state": "idle",
            "video_id":       "",
            "current_time":   0.0,
            "duration":       0.0,
        })
        self._report_state(also_now_playing=True)

    def _sync_time_from_mopidy(self):
        """Pull current position from Mopidy to keep Lounge in sync."""
        if self.core and self.state in ("1", "2"):
            try:
                ms = self.core.playback.get_time_position().get()
                if ms is not None:
                    self.current_time = _ms_to_sec(ms)
                    DialHandler.state["current_time"] = float(self.current_time)
            except Exception:
                pass

    def _sync_volume_from_mopidy(self):
        """Pull current volume from Mopidy."""
        if self.core:
            try:
                vol = self.core.mixer.get_volume().get()
                if vol is not None:
                    self.volume = vol
                mute = self.core.mixer.get_mute().get()
                if mute is not None:
                    self.muted = mute
            except Exception:
                pass
