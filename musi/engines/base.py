"""Engine-abstractie: één interface voor mpv en spotifyd.

De orchestrator praat alleen tegen de ``Engine``-ABC en hoeft niet te weten
welke backend speelt. Elke engine publiceert:
* **state-wijzigingen** via een callback (``set_state_callback``) → de UI ziet
  real-time status/positie/volume/track;
* **natuurlijk track-einde** via een tweede callback (``set_track_end_callback``)
  → de orchestrator schuift de queue door.

State-model: ``EngineState`` bevat status (stopped/playing/paused), positie,
duur, volume en de actieve track. ``EngineStatus`` is een enum. Beide zijn
immutable dataclasses (``replace`` voor wijzigingen) — engines bouwen telkens
een nieuwe state en emit 'm.

Levenscyclus: ``start`` → (play/pause/resume/seek/set_volume/stop) →
``shutdown``. Implementaties mogen lang lopen (mpv-subprocess, spotifyd-poll).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from enum import Enum
from typing import Callable

from ..models import Track


class EngineStatus(str, Enum):
    """Playback-status. (``str``-enum zodat ``.value`` vergelijkbaar is met
    engine-specifieke strings zoals ``"playing"``.)"""

    STOPPED = "stopped"
    PLAYING = "playing"
    PAUSED = "paused"


@dataclass
class EngineState:
    """Snapshot van de engine-state op een moment.

    Immutable; engines maken een nieuwe via ``replace`` en emit 'm. Default is
    "alles gestopt, geen track".
    """

    status: EngineStatus = EngineStatus.STOPPED
    position: float = 0.0   # seconden
    duration: float = 0.0   # seconden (0 = onbekend)
    volume: float = 100.0   # 0-100
    track: Track | None = None

    def with_status(self, status: EngineStatus) -> "EngineState":
        """Kopie met een gewijzigde status (handig in tests/transities)."""
        return replace(self, status=status)


StateCallback = Callable[["EngineState"], None]
TrackEndCallback = Callable[[], None]


class Engine(ABC):
    """Gemeenschappelijke interface voor playback-engines.

    Subclasses (``MpvEngine``, ``SpotifyEngine``) implementeren de abstracte
    methoden. De orchestrator koppelt de twee callbacks aan; engines roepen
    ``_emit_state``/``_emit_track_end`` aan wanneer er iets verandert.
    """

    name: str  # "mpv" | "spotifyd" — moet matchen met Track.engine

    def __init__(self) -> None:
        self._on_state: StateCallback | None = None
        self._on_track_end: TrackEndCallback | None = None

    # -- callbacks (door orchestrator gezet) --
    def set_state_callback(self, cb: StateCallback) -> None:
        """Koppel de state-callback (wordt aangeroepen bij elke state-wijziging)."""
        self._on_state = cb

    def set_track_end_callback(self, cb: TrackEndCallback) -> None:
        """Koppel de track-end-callback (bij natuurlijk einde van een track)."""
        self._on_track_end = cb

    def _emit_state(self, state: EngineState) -> None:
        """Stuur een state-update naar de callback (indien gekoppeld)."""
        if self._on_state is not None:
            self._on_state(state)

    def _emit_track_end(self) -> None:
        """Meld natuurlijk track-einde aan de callback (indien gekoppeld)."""
        if self._on_track_end is not None:
            self._on_track_end()

    # -- levenscyclus --
    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def shutdown(self) -> None: ...

    # -- playback --
    @abstractmethod
    async def play(self, track: Track) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def pause(self) -> None: ...

    @abstractmethod
    async def resume(self) -> None: ...

    @abstractmethod
    async def seek(self, seconds: float) -> None: ...

    @abstractmethod
    async def set_volume(self, volume: float) -> None: ...

    @abstractmethod
    async def state(self) -> EngineState: ...
