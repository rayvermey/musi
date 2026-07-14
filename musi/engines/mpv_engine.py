"""mpv-engine: aansturing via de JSON-IPC-socket (geen python-mpv nodig).

Start een headless ``mpv --idle`` met een IPC-socket en praat er asynchroon mee:
* commando's krijgen een ``request_id`` en worden via futures bevestigd;
* property-wijzigingen (``time-pos``, ``duration``, ``pause``, ``core-idle``,
  ``volume``) worden geobserveerd en drijven de EngineState;
* een ``end-file``-event met reason ``eof`` meldt natuurlijk track-einde aan de
  queue (zodat de orchestrator doorschuift).

Waarom IPC en niet python-mpv: geen native-binding nodig, robuust tegen
mpv-crashes (subprocess herstartbaar), en dezelfde socket kan eventueel door
andere tools worden aangestuurd.

Lokaal = ``loadfile <pad>``; YouTube = ``loadfile <watch-url>`` (mpv roept intern
yt-dlp aan voor de audio-stream). De engine draait **audio-only**
(``--no-video --audio-display=no``) — voor video is er de aparte V-toets-viewer
in ``app/musi_app.py`` die een tweede mpv met beeld start.

Throttle: de positie-voortgang wordt slechts ±2/sec doorgegeven (≥0.5s verschil),
i.p.v. bij elke mpv-frame-update (~10/sec). Dat houdt de UI vloeiend zonder de
event-stroom te overspoelen.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..models import Track
from .base import Engine, EngineState, EngineStatus

log = logging.getLogger(__name__)


class MpvError(RuntimeError):
    """mpv gaf een fout op een IPC-commando (``error != "success"``)."""


# (observe-id, property-name) — mpv stuurt een property-change-event bij elke
# wijziging. Deze vijf dekken alles wat de EngineState nodig heeft.
_OBSERVE = [
    (1, "time-pos"),
    (2, "duration"),
    (3, "pause"),
    (4, "core-idle"),
    (5, "volume"),
]


class MpvEngine(Engine):
    """Aansturing van één mpv-subprocess via JSON-IPC.

    Eénmaal ``start``-en opent de socket; daarna zijn ``play``/``pause``/enz.
    IPC-commando's. Responses worden gematcht op ``request_id`` via futures.
    Een reader-task verwerkt asynchrone events (property-changes, end-file).
    """

    name = "mpv"

    def __init__(self, socket_path: Path, mpv_bin: str = "mpv") -> None:
        """Args:
            socket_path: pad voor mpv's ``--input-ipc-server`` (onder cache_dir).
            mpv_bin: mpv-binary (standaard ``"mpv"`` op PATH).
        """
        super().__init__()
        self._socket_path = socket_path
        self._mpv_bin = mpv_bin
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._ids = itertools.count(1)            # monotoon stijgende request-ids
        self._pending: dict[int, asyncio.Future[Any]] = {}  # id → wachtend commando
        self._props: dict[str, Any] = {}          # laatst bekende property-waarden
        self._state = EngineState()
        self._last_status: EngineStatus | None = None
        self._last_emitted_pos: float = 0.0       # voor de positie-throttle

    # ---- levenscyclus -------------------------------------------------
    async def start(self) -> None:
        """Start het mpv-subprocess (audio-only, idle) en verbind met de socket.

        Maakt een eventuele oude socket weg, start mpv met ``--idle`` zodat 'm
        draait ook zónder geladen bestand, wacht tot de socket verschijnt (max
        ~10s), verbindt, en installeert de property-observers. mpv's stderr
        wordt op debug-niveau gedrained (handig bij problemen).
        """
        try:
            self._socket_path.unlink()
        except FileNotFoundError:
            pass
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)

        self._proc = await asyncio.create_subprocess_exec(
            self._mpv_bin,
            "--idle=yes",               # blijf draaien zonder bestand
            "--no-terminal",
            "--no-video",               # audio-only (de TUI toont geen beeld)
            "--audio-display=no",       # geen album-art-venster van mpv zelf
            f"--input-ipc-server={self._socket_path}",
            "--ytdl=yes",               # roep yt-dlp aan voor YouTube-URLs
            "--ytdl-format=bestaudio/best",  # audio-only voorkeur (zie module-doc)
            "--volume=100",
            "--gapless-audio=no",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr())

        # wacht tot de IPC-socket verschijnt (mpv maakt hem bij opstarten aan)
        for _ in range(100):  # max ~10 s
            if self._socket_path.exists():
                break
            if self._proc.returncode is not None:
                raise RuntimeError("mpv stopte direct na opstarten")
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError(f"mpv IPC-socket verscheen niet: {self._socket_path}")

        self._reader, self._writer = await asyncio.open_unix_connection(str(self._socket_path))
        self._reader_task = asyncio.create_task(self._read_loop())
        for pid, name in _OBSERVE:
            await self._send(["observe_property", pid, name])
        log.info("mpv-engine gestart (pid %s)", self._proc.pid)

    async def shutdown(self) -> None:
        """Sluit de socket, cancel reader/stderr-tasks, en beëindig mpv
        (eerst netjes terminate, dan kill als 'm niet binnen 3s stopt)."""
        if self._reader_task:
            self._reader_task.cancel()
        if self._stderr_task:
            self._stderr_task.cancel()
        try:
            if self._writer is not None:
                self._writer.close()
                await self._writer.wait_closed()
        except Exception:
            pass
        if self._proc is not None and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                self._proc.kill()

    async def _drain_stderr(self) -> None:
        """Leeg mpv's stderr regel-voor-regel naar het debug-log (voorkomt dat
        een volle pipe mpv blokkeert; geeft diagnose bij problemen)."""
        assert self._proc is not None and self._proc.stderr is not None
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                break
            log.debug("mpv stderr: %s", line.decode(errors="replace").rstrip())

    # ---- IPC-protocol ------------------------------------------------
    async def _send(self, command: list[Any], *, timeout: float = 5.0) -> Any:
        """Stuur één IPC-commando en wacht op het antwoord (gematcht op
        ``request_id``). Raise ``MpvError`` als mpv ``error != "success"``
        teruggeeft, of bij een time-out.

        Args:
            command: mpv-commando als lijst, bv ``["loadfile", url, "replace"]``.
            timeout: max wachttijd op antwoord.
        """
        if self._writer is None:
            raise RuntimeError("mpv-engine draait niet")
        rid = next(self._ids)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        self._pending[rid] = fut
        payload = json.dumps({"command": command, "request_id": rid}) + "\n"
        self._writer.write(payload.encode())
        await self._writer.drain()
        try:
            reply = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            raise
        err = reply.get("error", "success")
        if err != "success":
            raise MpvError(err)
        return reply.get("data")

    async def _read_loop(self) -> None:
        """Lees regel-voor-regel JSON-berichten van mpv en dispatch ze. Bij
        EOF/socket-fout stopt de loop netjes."""
        assert self._reader is not None
        while True:
            try:
                line = await self._reader.readline()
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                break
            if not line:
                break
            try:
                msg = json.loads(line.decode())
            except json.JSONDecodeError:
                continue
            self._dispatch(msg)

    def _dispatch(self, msg: dict[str, Any]) -> None:
        """Verwerk één mpv-bericht: óf een antwoord op een commando
        (``request_id``), óf een event (``property-change`` / ``end-file``)."""
        if "request_id" in msg:
            # antwoord op een eerder _send-commando → los de future op
            fut = self._pending.pop(msg["request_id"], None)
            if fut is not None and not fut.done():
                fut.set_result(msg)
            return
        event = msg.get("event")
        if event == "property-change":
            self._props[msg.get("name")] = msg.get("data")
            self._sync_state()
        elif event == "end-file":
            reason = msg.get("reason")
            self._state = replace(self._state, status=EngineStatus.STOPPED, position=0.0)
            self._emit_state(self._state)
            # Alleen bij natuurlijke einden (eof) schuift de queue door; bij een
            # bewuste stop/replace handelt de orchestrator het zelf.
            if reason == "eof":
                self._emit_track_end()

    def _sync_state(self) -> None:
        """Herbereken EngineState uit de geobserveerde properties en emit 'm.

        Status-afleiding uit ``pause`` en ``core-idle``:
          * ``pause == True`` → PAUSED;
          * ``core-idle == False`` (actief aan het decoderen/spelen) → PLAYING;
          * anders → STOPPED.

        Throttle: status-/volume-/duurwijzigingen gaan altijd door; bij louter
        positie-voortgang pas emitten bij ≥0.5s verschil (≈2/sec ipv ~10).
        """
        paused = self._props.get("pause")
        core_idle = self._props.get("core-idle")
        if paused is True:
            status = EngineStatus.PAUSED
        elif core_idle is False:
            status = EngineStatus.PLAYING
        else:
            status = EngineStatus.STOPPED
        volume = self._props.get("volume")
        position = self._props.get("time-pos") or 0.0
        duration = self._props.get("duration") or 0.0
        self._state = replace(
            self._state,
            status=status,
            position=position,
            duration=duration,
            volume=volume if isinstance(volume, (int, float)) else self._state.volume,
        )
        # Throttle: status-/volume-/duurwijzigingen altijd door; bij louter
        # positie-voortgang pas emitpen bij ≥0.5 s verschil (≈2/sec ipv ~10).
        status_changed = status != self._last_status
        pos_moved = abs(position - self._last_emitted_pos) >= 0.5
        if status_changed or pos_moved:
            self._last_status = status
            self._last_emitted_pos = position
            self._emit_state(self._state)

    # ---- playback (Engine-interface) ---------------------------------
    async def play(self, track: Track) -> None:
        """Laad ``track`` in mpv (``loadfile … replace``) en zet afspelen aan.

        ``uri`` is een bestandspad (lokaal) of een watch-URL (YouTube; mpv roept
        intern yt-dlp aan). De positie wordt vooraf gereset zodat de UI niet
        kort de vorige tijd toont.
        """
        self._state = replace(self._state, track=track)
        # reset positie zodat de UI niet kort de vorige tijd toont
        self._props.pop("time-pos", None)
        await self._send(["loadfile", track.uri, "replace"])
        await self._send(["set_property", "pause", False])
        self._emit_state(self._state)

    async def stop(self) -> None:
        """Stop de playback (geen queue-doorloop — de orchestrator beslist)."""
        await self._send(["stop"])
        self._state = replace(self._state, status=EngineStatus.STOPPED, position=0.0)
        self._emit_state(self._state)

    async def pause(self) -> None:
        """Pauzeer."""
        await self._send(["set_property", "pause", True])

    async def resume(self) -> None:
        """Hervat."""
        await self._send(["set_property", "pause", False])

    async def seek(self, seconds: float) -> None:
        """Seek naar een absolute positie (seconden)."""
        await self._send(["seek", float(seconds), "absolute"])

    async def set_volume(self, volume: float) -> None:
        """Zet het volume (0–100, geklemd)."""
        volume = max(0.0, min(100.0, float(volume)))
        await self._send(["set_property", "volume", volume])
        self._state = replace(self._state, volume=volume)
        self._emit_state(self._state)

    async def state(self) -> EngineState:
        """Huidige state (snapshot)."""
        return self._state
