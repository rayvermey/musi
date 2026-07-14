"""spotifyd-engine: aansturing via MPRIS over de session-bus (dasbus).

spotifyd draait als **externe** user-service; wij praten er alleen via dbus
mee. De bus-naam begint met ``org.mpris.MediaPlayer2.spotifyd`` (spotifyd kan er
een instance-suffix aan hangen, vandaar prefix-match — zie ``NAME_PREFIX``). We
gebruiken dasbus' ``get_proxy``, die het object introspecteert: methodes
(``Play``/``Pause``/``Next``/``Stop``/``SetPosition``) en properties
(``PlaybackStatus``/``Metadata``/``Volume``/``Position``) zijn dan direct
aanroepbaar.

State-reading gaat via een **poll-loop** (±2/sec) i.p.v. PropertiesChanged-signalen:
signal-subscription in dasbus is lastig, en 2/sec volstaat ruim voor een vloeiende
progress-bar.

Twee gotchas die dit module vorm geven (zie memory ``dasbus-mpris-gotchas``):
1. **Metadata (a{sv})** komt als ``GLib.Variant``-wrappers binnen; top-level
   properties daarentegen plain. Vandaar de ``_unwrap``-helper.
2. **MPRIS ``OpenUri`` werkt niet** in spotifyd 0.4.x voor track-URI's. Daarom
   starten we een track via de Spotify **Web API** ``start_playback`` (via de
   meegegeven spotipy-client-callable), niet via MPRIS. Pauze/hervat/seek/
   volume wél via MPRIS (die werken).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..models import Track
from .base import Engine, EngineState, EngineStatus

log = logging.getLogger(__name__)

OBJ_PATH = "/org/mpris/MediaPlayer2"
NAME_PREFIX = "org.mpris.MediaPlayer2.spotifyd"
POLL = 0.5  # seconden — poll-interval voor state-reading

# MPRIS PlaybackStatus-string → onze enum.
_STATUS_MAP = {
    "Playing": EngineStatus.PLAYING,
    "Paused": EngineStatus.PAUSED,
    "Stopped": EngineStatus.STOPPED,
}


def _unwrap(v: Any) -> Any:
    """Pak dasbus/GLib Variant-wrappers uit (Metadata a{sv}-waarden komen als
    Variant binnen, top-level properties daarentegen plain). Recursief voor
    lijsten en dicts, zodat de hele Metadata-structuur in één keer plain wordt."""
    if hasattr(v, "get_value"):
        v = v.get_value()
    elif hasattr(v, "unpack"):
        v = v.unpack()
    if isinstance(v, dict):
        return {str(k): _unwrap(val) for k, val in v.items()}
    if isinstance(v, list):
        return [_unwrap(x) for x in v]
    return v


def _meta_to_track(meta: dict[str, Any], fallback_uri: str) -> Track:
    """MPRIS Metadata-dict → Track. ``fallback_uri`` wordt gebruikt als de
    metadata geen ``mpris:trackid`` bevat (blijft dan de vorige uri staan)."""
    title = str(meta.get("xesam:title") or "")
    artists = meta.get("xesam:artist") or []
    artist = ", ".join(artists) if isinstance(artists, list) else str(artists or "")
    album_obj = meta.get("xesam:album")
    album = album_obj.get("title") if isinstance(album_obj, dict) else str(album_obj or "")
    length = meta.get("mpris:length")
    duration = float(length) / 1_000_000.0 if length else 0.0  # MPRIS gebruikt µs
    trackid = str(meta.get("mpris:trackid") or "")
    art = str(meta.get("mpris:artUrl") or "")
    return Track(
        source="spotify",
        engine="spotifyd",
        uri=trackid or fallback_uri,
        title=title or "(Spotify)",
        artist=artist,
        album=album,
        duration=duration,
        art_url=art,
        extra={"id": trackid},
    )


class SpotifyEngine(Engine):
    """Aansturing van spotifyd via MPRIS (session-bus) + Web API.

    De engine verbindt *zichzelf* (opnieuw) zodra spotifyd op de bus verschijnt —
    spotifyd hoeft dus nog niet te draaien bij ``start``. Pas als de proxy er is,
    pollt de loop de state. Playback-start gaat via de Web API (niet MPRIS).
    """

    name = "spotifyd"

    def __init__(self, spotify_provider=None, device_name: str = "musi-spotifyd") -> None:
        """Args:
            spotify_provider: callable die een ingelogde ``spotipy.Spotify``
                client oplevert (triggert OAuth bij eerste aanroep). Gebruikt
                voor ``start_playback`` via de Web API.
            device_name: naam van het spotifyd-apparaat (moet matchen met
                ``device_name`` in spotifyd.conf) — nodig om 'm in
                ``sp.devices()`` terug te vinden.
        """
        super().__init__()
        self._spotify_provider = spotify_provider  # callable → spotipy.Spotify (voor start_playback)
        self._device_name = device_name
        self._device_id: str | None = None
        self._bus = None
        self._proxy = None
        self._bus_name: str | None = None
        self._state = EngineState()
        self._stopped = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None
        self._last_sig: tuple | None = None  # laatst ge-emitte state-signatuur (dedup)
        # Een expliciete Stop() geeft ook PlaybackStatus=Stopped terug. Deze
        # vlag voorkomt dat zo'n handmatige stop als natuurlijk track-einde
        # de queue vooruit laat springen.
        self._explicit_stop_pending = False

    # ---- bus-verbinding ----------------------------------------------
    def _discover_name(self) -> str | None:
        """Zoek de spotifyd-bus-naam op de session-bus (prefix-match, want er
        kan een instance-suffix op staan)."""
        dbus = self._bus.get_proxy("org.freedesktop.DBus", "/org/freedesktop/DBus")
        for n in dbus.ListNames():
            if str(n).startswith(NAME_PREFIX):
                return str(n)
        return None

    def _connect_sync(self) -> None:
        """Verbind met spotifyd's MPRIS-object. Raise als spotifyd er nog niet
        is (de poll-loop vangt dat op en probeert opnieuw)."""
        from dasbus.connection import SessionMessageBus

        self._bus = SessionMessageBus()
        self._bus_name = self._discover_name()
        if self._bus_name is None:
            raise RuntimeError("spotifyd MPRIS niet op session-bus (nog niet via Connect ingelogd?)")
        self._proxy = self._bus.get_proxy(self._bus_name, OBJ_PATH)
        log.info("spotifyd-engine verbonden met %s", self._bus_name)

    # ---- state-reading -----------------------------------------------
    def _read_state_sync(self) -> EngineState:
        """Lees de Player-properties en bouw een EngineState. Variant-unwrap
        waar nodig; bij ontbreken van waarden valen we terug op de vorige
        state (zodat de UI niet flikkert)."""
        p = self._proxy
        status = _STATUS_MAP.get(str(_unwrap(p.PlaybackStatus)), EngineStatus.STOPPED)
        meta = _unwrap(dict(p.Metadata or {}))
        length = meta.get("mpris:length")
        duration = float(length) / 1_000_000.0 if length else self._state.duration
        fallback_uri = self._state.track.uri if self._state.track else ""
        track = _meta_to_track(meta, fallback_uri) if meta.get("mpris:trackid") else self._state.track
        try:
            volume = max(0.0, min(100.0, float(_unwrap(p.Volume) or 0.0) * 100.0))  # MPRIS volume is 0–1
        except Exception:
            volume = self._state.volume
        try:
            position = float(_unwrap(p.Position) or 0) / 1_000_000.0  # MPRIS positie is µs
        except Exception:
            position = self._state.position
        return EngineState(
            status=status, position=position, duration=duration, volume=volume, track=track
        )

    # ---- poll / retry-loop -------------------------------------------
    async def _loop(self) -> None:
        """Verbindt (opnieuw) zodra spotifyd er is en pollt dan elke ``POLL``
        seconden de state. Emit alleen bij wijziging (dedup via ``_last_sig``).

        Robuust tegen verdwijnende spotifyd: bij een poll-fout gaan we terug in
        "niet verbonden"-modus en probeert de loop opnieuw te verbinden.
        """
        while not self._stopped.is_set():
            if self._proxy is None:
                try:
                    await asyncio.to_thread(self._connect_sync)
                except Exception as e:
                    self._proxy = None
                    # stiltecyclussen voorkomen log-overstroom; alleen op debug
                    log.debug("spotifyd nog niet bereikbaar: %s", e)
            if self._proxy is not None:
                try:
                    previous = self._state
                    st = await asyncio.to_thread(self._read_state_sync)
                    # Spotify heeft geen apart EOF-signaal via MPRIS. Een
                    # Playing -> Stopped-overgang op dezelfde track is daarom
                    # het natuurlijke einde, behalve wanneer Stop() dit zelf
                    # heeft veroorzaakt. Een track zonder metadata/duration
                    # levert geen betrouwbaar einde-signaal op.
                    same_track = (
                        previous.track is not None
                        and st.track is not None
                        and previous.track.uri == st.track.uri
                    )
                    reached_end = (
                        same_track
                        and previous.status is EngineStatus.PLAYING
                        and st.status is EngineStatus.STOPPED
                        and previous.duration > 0
                        and previous.position >= max(0.0, previous.duration - 1.5)
                    )
                    explicit_stop = self._explicit_stop_pending
                    self._explicit_stop_pending = False
                    # signatuur om te detecteren of er iets veranderd is (zonder
                    # elke positie-microbeweging te emitten)
                    sig = (
                        st.status,
                        round(st.position),
                        round(st.duration),
                        st.track.uri if st.track else "",
                    )
                    if sig != self._last_sig:
                        self._last_sig = sig
                        self._state = st
                        self._emit_state(st)
                    if reached_end and not explicit_stop:
                        self._emit_track_end()
                except Exception as e:
                    log.debug("spotifyd poll-fout (proxy weg?): %s", e)
                    self._proxy = None  # forceer herverbinden
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=POLL)
            except asyncio.TimeoutError:
                pass

    # ---- levenscyclus ------------------------------------------------
    async def start(self) -> None:
        """Start de poll-loop (spotifyd zelf hoeft er nog niet te zijn)."""
        self._stopped.clear()
        self._loop_task = asyncio.create_task(self._loop())
        log.info("spotifyd-engine started (poll-loop actief)")

    async def shutdown(self) -> None:
        """Stop de poll-loop en vergeet de proxy."""
        self._stopped.set()
        if self._loop_task is not None:
            self._loop_task.cancel()
        self._proxy = None

    def _require(self):
        """Geef de proxy, of raise als we niet verbonden zijn (beschermt de
        playback-methoden — roep de Web API-flow voor ``play``)."""
        if self._proxy is None:
            raise RuntimeError("spotifyd niet verbonden (log eerst in via Spotify Connect)")
        return self._proxy

    # ---- playback (Engine-interface) ---------------------------------
    # ---- playback-start via Web API (betrouwbaar; MPRIS OpenUri is stuk in 0.4.x)
    def _resolve_device_id(self, sp) -> str:
        """Vind het spotifyd-apparaat in ``sp.devices()`` op naam. Cachet 'm
        (``_device_id``) zodat we niet elke keer hoeven te zoeken. Raise met een
        behulpzaam bericht als 'm er niet is (bijv. spotifyd niet ingelogd)."""
        if self._device_id:
            return self._device_id
        devices = (sp.devices() or {}).get("devices") or []
        for d in devices:
            if (d.get("name") or "").lower() == self._device_name.lower():
                self._device_id = d.get("id")
                return self._device_id  # type: ignore[return-value]
        names = [d.get("name") for d in devices]
        raise RuntimeError(
            f"Spotify-device {self._device_name!r} niet gevonden in Connect-devices {names}. "
            "Is spotifyd ingelogd (via Spotify Connect) en zichtbaar?"
        )

    async def play(self, track: Track) -> None:
        """Start ``track`` via de Web API ``start_playback``.

        We gebruiken níet MPRIS ``OpenUri`` (die is stuk in spotifyd 0.4.x —
        geeft ``Invalid state`` / ``InvalidRoot``). In plaats daarvan vragen we
        de spotipy-client om op het spotifyd-device ``uris=[track.uri]`` te
        starten. De optimistic state-update (PLAYING) geeft de UI direct
        feedback; de poll-loop corrigeert 'm zodra spotifyd de echte state emit.
        """
        uri = track.uri
        provider = self._spotify_provider

        def call() -> None:
            if provider is None:
                raise RuntimeError("geen spotipy-client gekoppeld aan de spotifyd-engine")
            sp = provider()  # triggert OAuth bij eerste keer
            device_id = self._resolve_device_id(sp)
            sp.start_playback(device_id=device_id, uris=[uri])

        await asyncio.to_thread(call)
        self._state = EngineState(
            status=EngineStatus.PLAYING, track=track,
            position=0.0, duration=track.duration or self._state.duration,
            volume=self._state.volume,
        )
        self._emit_state(self._state)

    async def stop(self) -> None:
        """Stop (via MPRIS ``Stop``)."""
        self._explicit_stop_pending = True
        try:
            await asyncio.to_thread(lambda: self._require().Stop())  # type: ignore[attr-defined]
        except Exception:
            self._explicit_stop_pending = False
            raise

    async def pause(self) -> None:
        """Pauzeer (via MPRIS ``Pause``)."""
        await asyncio.to_thread(lambda: self._require().Pause())  # type: ignore[attr-defined]

    async def resume(self) -> None:
        """Hervat (via MPRIS ``Play``)."""
        await asyncio.to_thread(lambda: self._require().Play())  # type: ignore[attr-defined]

    async def seek(self, seconds: float) -> None:
        """Seek naar een absolute positie (via MPRIS ``SetPosition``; µs)."""
        def call() -> None:
            p = self._require()
            tid = self._state.track.uri if self._state.track else "/org/mpris/MediaPlayer2/TrackList/NoTrack"
            p.SetPosition(tid, int(seconds * 1_000_000))  # type: ignore[attr-defined]

        await asyncio.to_thread(call)

    async def set_volume(self, volume: float) -> None:
        """Zet het volume (0–100 → MPRIS 0–1, geklemd; via property-set)."""
        vol = max(0.0, min(100.0, float(volume))) / 100.0
        await asyncio.to_thread(lambda: setattr(self._require(), "Volume", vol))

    async def state(self) -> EngineState:
        """Huidige state (snapshot)."""
        return self._state
