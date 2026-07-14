"""Orchestrator — koppelt engines aan de queue en stuurt de playback.

Verantwoordelijkheden (één plek dat de playback-redenering herbergt):
* **één uniforme queue** waarin tracks van álle bronnen belanden (lokaal,
  YouTube, Spotify door elkaar);
* **engine-keuze per track** — op basis van ``track.engine`` wordt de juiste
  engine gekozen (mpv voor lokaal/YouTube, spotifyd voor Spotify) en de vorige
  engine uitgezet, zodat er nooit twee audiostromen tegelijk botsen ("handoff");
* **engine-state → UI** — elke engine emit EngineState-wijzigingen; de
  orchestrator stuurt die door als één brede ``PlaybackEvent``-stroom naar de UI;
* **volume-routering** — alleen de actieve engine krijgt volumewijzigingen
  (andere engines behouden hun volume zodat ze later schokloos terugkeren);
* **natuurlijk track-einde** — een "track afgelopen"-signaal van een engine
  schuift de queue door naar de volgende, of stopt als de queue op is.

De orchestrator is bewust engine-agnostic: hij praat alleen tegen de
``Engine``-ABC (``engines/base.py``) en weet niet hoe mpv of spotifyd werken.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Callable

from .engines.base import Engine, EngineState
from .engines.mpv_engine import MpvEngine
from .queue import Queue

log = logging.getLogger(__name__)


@dataclass
class PlaybackEvent:
    """Wat de UI ontvangt — één brede event-stroom.

    Attributen zijn optioneel; afhankelijk van ``kind`` is het relevante veld
    gevuld. Soorten:
      * ``"state"`` — playback-status gewijzigd (``state`` gevuld);
      * ``"queue"`` — queue gewijzigd (``queue`` gevuld);
      * ``"error"`` — een engine-fout (``error`` gevuld met bericht).
    """

    kind: str  # "state" | "queue" | "error"
    state: EngineState | None = None
    queue: Queue | None = None
    error: str = ""


EventCallback = Callable[[PlaybackEvent], None]


class Orchestrator:
    """Houdt engines + queue bij en vertaalt UI-acties naar engine-calls.

    De UI spreekt de orchestrator aan (``play_index``, ``toggle_pause``,
    ``queue_add``, …); de orchestrator spreekt de engines aan. Engines koppelen
    hun state/track-end-callbacks terug aan de orchestrator (gedaan in
    ``__init__``), zodat natuurlijke track-eindes en state-updates hier samenkomen.
    """

    def __init__(
        self,
        engines: dict[str, Engine],
        event_cb: EventCallback | None = None,
    ) -> None:
        """Args:
            engines: mapping engine-naam → Engine. Namen: ``"mpv"`` en
                optioneel ``"spotifyd"``. De orchestrator kiest per track op
                ``track.engine``.
            event_cb: optionele callback voor PlaybackEvents (UI koppelt 'm).
        """
        # engine-naam ("mpv"/"spotifyd") → instance
        self._engines: dict[str, Engine] = engines
        self._event_cb = event_cb
        self.queue = Queue()
        self.state: EngineState = EngineState()
        # Monotonisch oplopende teller die per nieuwe playback omhoog gaat.
        # Geplande ``_on_track_end``-taken leggen de waarde vast bij scheduling
        # en vergelijken 'm opnieuw bij het uitvoeren — als een ``play_index``
        # of expliciete ``stop`` ertussen de teller heeft opgehoogd, baalt de
        # stale taak (uit een oude generatie) en springt de queue niet
        # onnodig verder.
        self._playback_generation = 0
        # Koppel state-callback van elke engine aan onze event-stream
        for name, eng in self._engines.items():
            eng.set_state_callback(lambda s, n=name: self._on_engine_state(n, s))
            eng.set_track_end_callback(lambda: self._on_track_end())

    # ---- event glue --------------------------------------------------
    def _emit(self, event: PlaybackEvent) -> None:
        """Stuur een PlaybackEvent naar de UI (als 'm een callback koppelde)."""
        if self._event_cb is not None:
            self._event_cb(event)

    def _on_engine_state(self, engine_name: str, state: EngineState) -> None:
        """Een engine emit een nieuwe state — onthoud 'm en stuur door."""
        self.state = state
        self._emit(PlaybackEvent(kind="state", state=state))

    def _on_track_end(self) -> None:
        """Plan de overgang na een natuurlijk einde.

        Engine-callbacks kunnen uit een oude playback-generatie komen. De
        geplande taak controleert daarom opnieuw of de queue nog actief is
        voordat hij een volgende track start.
        """
        generation = self._playback_generation
        log.info("track-einde → volgende")

        async def advance() -> None:
            await asyncio.sleep(0)
            if generation != self._playback_generation or not self.queue.tracks:
                return
            if self.queue.has_next():
                await self.play_index(self.queue.index + 1)
            else:
                eng_name = self._active_engine_name()
                if eng_name is not None:
                    try:
                        await self._engines[eng_name].stop()
                    except Exception:
                        log.debug("stop na track-einde mislukt", exc_info=True)

        try:
            asyncio.get_running_loop().create_task(advance())
        except RuntimeError:
            log.debug("track-einde zonder actieve event-loop genegeerd")

    # ---- engine-keuze -----------------------------------------------
    def _active_engine_name(self) -> str | None:
        """Naam van de engine van de actieve track, of ``None`` als er niets
        speelt (gebaseerd op de laatst bekende state)."""
        if self.state.track is None:
            return None
        return self.state.track.engine

    def _engine_for(self, track) -> Engine:
        """De engine die deze track moet spelen (op basis van ``track.engine``)."""
        return self._engines[track.engine]

    async def _ensure_active(self, track) -> None:
        """Zet de juiste engine op 'actief': start zonodig de engine en zet de
        vorige engine uit zodat er maar één tegelijk audio maakt.

        We stoppen alle *andere* engines dan de doel-engine. Fouten daarbij
        loggen we op debug-niveau (een niet-actieve engine die al uit staat
        mag geen fatale fout geven).
        """
        target_name = track.engine
        for name, eng in self._engines.items():
            if name == target_name:
                continue
            # niet de doelengine: stoppen zodat er geen ghost-audio ontstaat
            try:
                await eng.stop()
            except Exception as e:
                log.debug("fout bij stoppen van niet-actieve engine %s: %s", name, e)

    # ---- publieke acties (UI roept deze aan) -------------------------
    async def play_index(self, i: int) -> None:
        """Speel de track op positie ``i`` (en update ``queue.index``).

        Buiten bereik → no-op. Bij een engine-fout emit we een error-event
        i.p.v. te crashen (zodat de UI 'm kan tonen en de app draaiende blijft).

        De ``_playback_generation`` wordt hier opgehoogd zodat een eventueel
        nog openstaande ``_on_track_end``-taak uit de **vorige** track ziet
        dat hij niet meer aan de beurt is — anders zou een natuurlijk einde
        vlak vóór een handmatige ``next()`` de queue dubbel doorschuiven.
        """
        if not (0 <= i < len(self.queue.tracks)):
            return
        self._playback_generation += 1
        track = self.queue.tracks[i]
        self.queue.index = i
        await self._ensure_active(track)
        try:
            await self._engine_for(track).play(track)
        except Exception as e:
            log.error("play(%s) mislukte: %s", track.uri, e)
            self._emit(PlaybackEvent(kind="error", error=str(e)))

    async def play_first(self) -> None:
        """Speel de eerste track in de queue (als 'm niet leeg is)."""
        if self.queue.tracks:
            await self.play_index(0)

    async def next(self) -> None:
        """Speel de volgende track (als 'm er is)."""
        if self.queue.has_next():
            await self.play_index(self.queue.index + 1)

    async def prev(self) -> None:
        """Speel de vorige track (als 'm er is)."""
        if self.queue.has_prev():
            await self.play_index(self.queue.index - 1)

    async def toggle_pause(self) -> None:
        """Wissel pauze/hervat op de actieve engine.

        Belangrijk: als er niets speelt (geen actieve track) doen we **niets** —
        we beginnen níet stiekem queue[0]. Dat gaf vroeger het verrassende
        "spatie speelt steeds hetzelfde nummer"-effect vlak ná ``play_track``,
        vóór het eerste state-poll (state.track was nog None)."""
        eng_name = self._active_engine_name()
        if eng_name is None:
            # Er speelt niets — spatie mag niet stiekem het eerste nummer
            # (her)starten; dat gaf het verrassende "spatie speelt steeds het
            #zelfde nummer"-effect vlak ná play_track, vóór het eerste
            # state-poll (state.track was nog None).
            log.debug("toggle_pause: geen actieve track — negeer")
            return
        eng = self._engines[eng_name]
        if self.state.status.value == "playing":
            await eng.pause()
        else:
            await eng.resume()

    async def stop(self) -> None:
        """Stop álles — alle engines uit. Hoogt ook de playback-generatie op
        zodat een openstaande ``_on_track_end``-taak uit een vorige generatie
        zichzelf annuleert (anders zou die alsnog de queue doorschuiven)."""
        self._playback_generation += 1
        for eng in self._engines.values():
            try:
                await eng.stop()
            except Exception:
                pass

    async def seek(self, seconds: float) -> None:
        """Seek de actieve engine naar een absolute positie (seconden)."""
        eng_name = self._active_engine_name()
        if eng_name is None:
            return
        await self._engines[eng_name].seek(seconds)

    async def set_volume(self, vol: float) -> None:
        """Zet het volume op de actieve engine (0–100). Andere engines behouden
        hun eigen volume zodat ze later schokloos terugkeren."""
        eng_name = self._active_engine_name()
        if eng_name is None:
            return
        await self._engines[eng_name].set_volume(vol)

    # ---- queue-helpers (triggeren één queue-event) -------------------
    def queue_add(self, track) -> None:
        """Voeg één track toe aan de queue (emit één queue-event)."""
        self.queue.append(track)
        self._emit_queue_event()

    def queue_extend(self, tracks) -> None:
        """Voeg meerdere tracks toe aan de queue — **één** queue-event i.p.v. N
        (handig voor "voeg heel album/map/artiest toe")."""
        self.queue.extend(tracks)
        self._emit_queue_event()

    def queue_insert_next(self, track) -> None:
        """Voeg een track direct ná de huidige in ("als volgende")."""
        self.queue.insert_next(track)
        self._emit_queue_event()

    def queue_remove(self, i: int) -> None:
        """Verwijder de track op positie ``i``."""
        self.queue.remove_at(i)
        self._emit_queue_event()

    def queue_clear(self) -> None:
        """Wis de hele queue. Hoogt de playback-generatie op zodat een nog
        openstaande ``_on_track_end``-taak uit een vorige generatie zichzelf
        annuleert (de check ``not self.queue.tracks`` vangt de meeste gevallen
        maar een gelijktijdige nieuwe ``play_index`` zou anders nog kunnen
        doorschieten)."""
        self._playback_generation += 1
        self.queue.clear()
        self._emit_queue_event()

    async def play_all(self, tracks) -> None:
        """Vervang de queue door ``tracks`` en speel de eerste.

        Eén queue-event in plaats van N (``queue_add`` emit per track). Herbruikbaar
        voor "speel hele map / album / artiest" (de ``A``-binding in de UI)."""
        self.queue.clear()
        self.queue.extend(tracks)
        self._emit_queue_event()
        if tracks:
            await self.play_index(0)

    def _emit_queue_event(self) -> None:
        """Stuur een queue-gewijzigd-event naar de UI."""
        self._emit(PlaybackEvent(kind="queue", queue=self.queue))

    # ---- levenscyclus ------------------------------------------------
    async def start(self) -> None:
        """Start alle engines (mpv-subprocess, spotifyd-poll-loop)."""
        for eng in self._engines.values():
            await eng.start()

    async def shutdown(self) -> None:
        """Sluit alle engines netjes af (opgeroepen in cli's finally)."""
        for eng in self._engines.values():
            try:
                await eng.shutdown()
            except Exception:
                pass
