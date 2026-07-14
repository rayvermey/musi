"""musi.cli — commando-entry-point (console-script ``musi``).

Twee taken:
1. ``musi doctor`` — print de effectieve config + setup-status (geen TUI, geen
   zware imports). Handig om te checken of paden/client_id kloppen voordat je de
   TUI start.
2. ``musi`` (zonder subcommando) — bouwt de Services op, koppelt de engines aan
   de Orchestrator, en start de Textual-TUI (``MusiApp``). Zie ``services.py``
   voor de bedrading en ``app/musi_app.py`` voor de UI.

De Textual-import staat bewust *binnen* ``_run_tui`` en niet op module-niveau:
zodoende start ``musi doctor`` snel zonder Textual (en z'n afhankelijkheden) te
laden.

Lifecycle: ``orch.start()`` start alle engines (mpv subprocess, spotifyd
poll-loop); ``app.run_async()`` blokkeert tot de gebruiker quit; in de
``finally`` worden engines netjes afgesloten en de sqlite-verbinding gesloten
(zodat een nooit-afgesloten mpv of een stale DB-lock achterblijft).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from . import __version__
from .config import load
from .orchestrator import Orchestrator
from .services import build_full


def _cmd_doctor() -> int:
    """``musi doctor`` — toon de effectieve paden + Spotify-config.

    Geen netwerk- of engine-acties; pure config-introspectie. Returnt 0.
    """
    cfg = load()
    print(f"musi {__version__}")
    print(f"  music_dir          : {cfg.music_dir}")
    print(f"  cache_dir          : {cfg.cache_dir}")
    print(f"  library_db         : {cfg.library_db}")
    print(f"  mpv_socket         : {cfg.mpv_socket}")
    print(f"  spotify_client_id  : {cfg.spotify_client_id or '(niet ingesteld)'}")
    print(f"  spotify_redirect   : {cfg.spotify_redirect_uri}")
    return 0


async def _run_tui() -> int:
    """Bouw alles op en start de TUI (async entry-point).

    Stappen:
      1. laad config;
      2. ``build_full`` maakt de engines + zoekproviders + library aan;
      3. koppel engines aan de orchestrator (mpv altijd; spotifyd als 'm er is);
      4. start de engines, run de app, en sluit in ``finally`` alles af.

    De Textual-import gebeurt hier (niet top-level) om ``musi doctor`` snel te
    houden.
    """
    from .app.musi_app import MusiApp

    cfg = load()
    sv = build_full(cfg)
    # Engines-dict: mpv is altijd aanwezig (lokaal + YouTube). spotifyd alleen
    # als 'm gebouwd is — ontbreekt 'm, dan draait musi zonder Spotify en de
    # orchestrator kiest per track de juiste engine (of faalt duidelijk bij een
    # Spotify-track zonder spotifyd).
    engines = {"mpv": sv.mpv}
    if sv.spotify_engine is not None:
        engines["spotifyd"] = sv.spotify_engine
    orch = Orchestrator(engines)
    app = MusiApp(orch, sv, cfg)

    try:
        await orch.start()        # start mpv-subprocess + spotifyd-poll
        await app.run_async()     # blokkeert tot quit
    finally:
        await orch.shutdown()     # stop mpv/spotifyd netjes
        sv.library.close()        # sluit sqlite-verbinding (geen stale lock)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Top-level entry: argparse + logging-instel + dispatch naar doctor/TUI.

    Args:
        argv: optionele argumenten-lijst (voor testen); ``None`` = ``sys.argv``.

    Returns:
        Process exit-code (0 = OK, 130 = afgebroken met Ctrl-C).
    """
    parser = argparse.ArgumentParser(
        prog="musi", description="Alles-in-één muziek-TUI (lokaal + YouTube + Spotify)"
    )
    parser.add_argument("--version", action="version", version=f"musi {__version__}")
    parser.add_argument("--log", default="WARNING", help="log-level (DEBUG/INFO/WARNING/ERROR)")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("doctor", help="toon config + setup-status")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log.upper(), logging.WARNING),
        format="%(asctime)s %(levelname)8s %(name)s: %(message)s",
    )

    if args.cmd == "doctor":
        return _cmd_doctor()

    try:
        return asyncio.run(_run_tui())
    except KeyboardInterrupt:
        # 130 is de conventionele exit-code voor "afgebroken met SIGINT"
        return 130


if __name__ == "__main__":
    sys.exit(main())
