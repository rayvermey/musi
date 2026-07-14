"""Regressie-tests voor ``musi/orchestrator.py``.

Achtergrond: op 14 jul 2026 had een wijziging in ``_on_track_end`` een
``_playback_generation`` teller geïntroduceerd om stale track-end-taken te
annuleren, maar de teller werd nergens opgehoogd — waardoor een natuurlijk
einde vlak vóór een handmatige ``next()`` de queue dubbel doorschoof.

Deze tests verifiëren de fix: bump in ``play_index``/``stop``/``queue_clear``
+ de staleness-check in ``advance``.
"""
from __future__ import annotations

import asyncio

from musi.engines.base import EngineState, EngineStatus
from musi.models import Track
from musi.orchestrator import Orchestrator


class _MockEng:
    """Minimale engine-stub: onthoudt alleen wat er gespeeld is."""

    name = "mpv"

    def __init__(self) -> None:
        self.state = EngineState()
        self.played: list[str] = []
        self._state_cb = None
        self._te_cb = None

    # Engine-interface
    async def play(self, t):
        new = EngineState(status=EngineStatus.PLAYING, track=t, position=0.0, duration=t.duration)
        self.state = new
        if self._state_cb:
            self._state_cb(new)
        self.played.append(t.uri)

    async def stop(self):
        self.state = EngineState()

    async def pause(self): self.state = self.state.with_status(EngineStatus.PAUSED)
    async def resume(self): self.state = self.state.with_status(EngineStatus.PLAYING)
    async def seek(self, s): pass
    async def set_volume(self, v): pass
    async def start(self): pass
    async def shutdown(self): pass
    async def state(self): return self.state

    def set_state_callback(self, cb): self._state_cb = cb
    def set_track_end_callback(self, cb): self._te_cb = cb

    # Test-helper: boots een natuurlijk einde na (engine stuurt eerst
    # state(STOPPED) en direct daarna track_end — synchroon, zoals
    # spotifyd-engine doet in z'n poll-loop).
    def emit_natural_end(self, track):
        st = EngineState(status=EngineStatus.STOPPED, track=track, position=0.0, duration=track.duration)
        self.state = st
        if self._state_cb:
            self._state_cb(st)
        if self._te_cb:
            self._te_cb()


def _tracks(n: int) -> list[Track]:
    return [
        Track(source="local", engine="mpv", uri=f"t{i}", title=f"T{i}", duration=180.0)
        for i in range(n)
    ]


async def _run(coro):
    return await coro


# ---- natuurlijk einde schuift door ------------------------------------
def test_natural_end_advances_to_next():
    async def go():
        eng = _MockEng()
        o = Orchestrator({"mpv": eng})
        await o.play_all(_tracks(3))
        await asyncio.sleep(0)
        eng.emit_natural_end(_tracks(3)[0])
        await asyncio.sleep(0.05)
        assert eng.played == ["t0", "t1"], eng.played
        assert o.queue.index == 1
    asyncio.run(go())


def test_natural_end_at_end_of_queue_stops_engine():
    async def go():
        eng = _MockEng()
        o = Orchestrator({"mpv": eng})
        await o.play_all(_tracks(2))
        await asyncio.sleep(0)
        # t1 (laatste) eindigt — engine moet stoppen, geen t2-sprong
        eng.emit_natural_end(_tracks(2)[1])
        await asyncio.sleep(0.05)
        assert eng.played == ["t0", "t1"], eng.played
        assert o.queue.index == 1
    asyncio.run(go())


# ---- stale-taak-detectie (de fix) ------------------------------------
def test_manual_next_during_pending_track_end_does_not_double_advance():
    """Race: t0 eindigt natuurlijk (advance staat open) → user drukt 'n'
    vóór advance loopt. Zonder fix schoof de queue twee keer door; met
    fix annuleert de bump in play_index de stale advance."""
    async def go():
        eng = _MockEng()
        o = Orchestrator({"mpv": eng})
        await o.play_all(_tracks(4))
        await asyncio.sleep(0)
        # 1) t0 eindigt natuurlijk: engine stuurt state(STOPPED,t0) + track_end
        eng.emit_natural_end(_tracks(4)[0])
        # 2) vóór advance() loopt, drukt de user 'n' → play_index(1)
        await o.next()
        # 3) nu zou de stale advance uit stap 1 alsnog play_index(2) doen
        await asyncio.sleep(0.05)
        assert eng.played == ["t0", "t1"], eng.played
        assert o.queue.index == 1
    asyncio.run(go())


def test_queue_clear_during_pending_track_end_cancels_advance():
    async def go():
        eng = _MockEng()
        o = Orchestrator({"mpv": eng})
        await o.play_all(_tracks(3))
        await asyncio.sleep(0)
        eng.emit_natural_end(_tracks(3)[0])
        o.queue_clear()                       # bump + leeg
        await asyncio.sleep(0.05)
        assert eng.played == ["t0"], eng.played
        assert o.queue.index == -1
    asyncio.run(go())


def test_stop_during_pending_track_end_cancels_advance():
    async def go():
        eng = _MockEng()
        o = Orchestrator({"mpv": eng})
        await o.play_all(_tracks(3))
        await asyncio.sleep(0)
        eng.emit_natural_end(_tracks(3)[0])
        await o.stop()                         # bump + stop alle engines
        await asyncio.sleep(0.05)
        assert eng.played == ["t0"], eng.played
    asyncio.run(go())


def test_play_all_resets_generation_so_pending_advance_is_stale():
    """play_all verwijdert de queue en start track 0 opnieuw. Een openstaande
    advance uit een eerdere playback moet niet alsnog doorschuiven."""
    async def go():
        eng = _MockEng()
        o = Orchestrator({"mpv": eng})
        await o.play_all(_tracks(3))
        await asyncio.sleep(0)
        eng.emit_natural_end(_tracks(3)[0])        # pending advance(0)
        await o.play_all(_tracks(2))                # wis + zet 2 nieuwe tracks
        await asyncio.sleep(0.05)
        # gespeeld: t0 (eerste play_all) + t0' (nieuwe play_all) — geen t1'
        assert eng.played == ["t0", "t0"], eng.played
        assert o.queue.index == 0
    asyncio.run(go())