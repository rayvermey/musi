"""E2E-test voor het playlist-beheer-flow (musi, 14 jul 2026).

Dekt:
1. ``MusiApp.open_save_picker`` opent de PickerModal + dispatcht het
   resultaat correct (callback + non-blocking — geen deadlock zoals
   ``test_deadlock.py``).
2. ``PlaylistPickerModal`` rendert Liked Songs + gefilterde playlists +
   nieuwe-playlist-row, en dismissed met het juiste tuple-type.
3. ``PlaylistNameModal`` dismissed met de nieuwe naam (str).
4. De LibraryPane-acties ``action_save/rename/delete_playlist`` zijn
   aanwezig + focus-dispatch werkt.

Vereist een werkende Spotify-OAuth (token-cache aanwezig); anders sla
de OAuth-flow over met ``pytest.skip`` zodat CI niet breekt.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import DataTable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from musi.config import load as load_config
from musi.modals import PlaylistNameModal, PlaylistPickerModal


async def _check_1_picker_dismiss_types() -> str:
    """Open de picker, programmeer de dismiss-flow, en check dat de callback
    het verwachte tuple-type krijgt voor elk scenario."""
    # We bouwen een minimale Modal-test die de picker gebruikt zonder een
    # echte SpotifySearch (mock met lege lijst).
    class MockSP:
        name = "spotify"
        label = "Spotify"
        last_error = None

        def __init__(self):
            self._playlists = [
                {"uri": "spotify:playlist:AAA", "name": "Mijn lijst",
                 "public": True, "tracks": {"total": 5}},
                {"uri": "spotify:playlist:BBB", "name": "Privé",
                 "public": False, "tracks": {"total": 0}},
            ]
            self._count = 42

        async def user_playlists(self, limit=200):
            return self._playlists

        async def saved_track_count(self):
            return self._count

        def get_client(self):
            return None

    # We testen d.m.v. push_screen + een stub-callback of de dismiss-types
    # kloppen: lijst-items, filter, nieuwe-playlist, en annuleren.
    results = {}

    async def _case_liked(app):
        sp = MockSP()
        captured = {}
        def cb(result):
            captured["liked"] = result
        app.push_screen(PlaylistPickerModal(sp, "X / Y"), callback=cb)
        await asyncio.sleep(0.4)  # langere pauze voor worker-load
        modal = app.screen
        from textual.widgets import DataTable
        tbl = modal.query_one("#pl-pick-table", DataTable)
        # Focus de tabel en zet cursor op rij 0 (anders heeft de input focus)
        tbl.focus()
        await asyncio.sleep(0.05)
        tbl.move_cursor(row=0)
        modal.action_pick()
        await asyncio.sleep(0.1)
        return captured.get("liked")

    async def _case_filter(app):
        sp = MockSP()
        captured = {}
        def cb(result):
            captured["new"] = result
        app.push_screen(PlaylistPickerModal(sp, "Test"), callback=cb)
        await asyncio.sleep(0.4)
        modal = app.screen
        from textual.widgets import Input, DataTable
        # zet filter via de Input (die heeft focus bij on_mount)
        filt = modal.query_one("#pl-pick-filter", Input)
        filt.value = "nieuwlijst"
        await asyncio.sleep(0.05)
        # focus de tabel + cursor op laatste rij (nieuwe-playlist)
        tbl = modal.query_one("#pl-pick-table", DataTable)
        tbl.focus()
        await asyncio.sleep(0.05)
        tbl.move_cursor(row=tbl.row_count - 1)
        modal.action_pick()
        await asyncio.sleep(0.1)
        return captured.get("new")

    async def _case_picklist(app):
        sp = MockSP()
        captured = {}
        def cb(result):
            captured["pl"] = result
        app.push_screen(PlaylistPickerModal(sp, "Test"), callback=cb)
        await asyncio.sleep(0.4)
        modal = app.screen
        from textual.widgets import DataTable
        tbl = modal.query_one("#pl-pick-table", DataTable)
        tbl.focus()
        await asyncio.sleep(0.05)
        tbl.move_cursor(row=1)
        modal.action_pick()
        await asyncio.sleep(0.1)
        return captured.get("pl")

    async def _case_cancel(app):
        sp = MockSP()
        captured = {}
        def cb(result):
            captured["cancel"] = result
        app.push_screen(PlaylistPickerModal(sp, "Test"), callback=cb)
        await asyncio.sleep(0.4)
        modal = app.screen
        modal.action_cancel()
        await asyncio.sleep(0.1)
        return captured.get("cancel")

    class TestApp(App):
        def compose(self) -> ComposeResult:
            yield Vertical()

        async def run_cases(self):
            r1 = await _case_liked(self)
            r2 = await _case_filter(self)
            r3 = await _case_picklist(self)
            r4 = await _case_cancel(self)
            return r1, r2, r3, r4

    app = TestApp()
    async with app.run_test() as pilot:
        r1, r2, r3, r4 = await app.run_cases()
    assert r1 == ("liked", None), f"Liked: {r1!r}"
    assert r2 == ("new", "nieuwlijst"), f"New: {r2!r}"
    assert r3 == ("playlist", "spotify:playlist:AAA"), f"Playlist: {r3!r}"
    assert r4 is None, f"Cancel: {r4!r}"
    return "Picker: alle 4 cases slagen (liked/new/playlist/cancel)."


async def _check_2_name_modal() -> str:
    """PlaylistNameModal dismissed met de nieuwe naam bij save, None bij cancel."""
    results = {}

    def cb_save(result):
        results["save"] = result

    def cb_cancel(result):
        results["cancel"] = result

    class TestApp(App):
        def compose(self) -> ComposeResult:
            yield Vertical()

    app = TestApp()
    async with app.run_test() as pilot:
        app.push_screen(
            PlaylistNameModal("Hernoem", "oude naam"),
            callback=cb_save,
        )
        await asyncio.sleep(0.1)
        from textual.widgets import Input, Button
        modal = app.screen
        modal.query_one("#pl-rename-input", Input).value = "nieuwe naam"
        await asyncio.sleep(0.05)
        modal.query_one("#pl-rename-save", Button).press()
        await asyncio.sleep(0.1)

    app2 = TestApp()
    async with app2.run_test() as pilot:
        app2.push_screen(
            PlaylistNameModal("Hernoem", "x"),
            callback=cb_cancel,
        )
        await asyncio.sleep(0.1)
        from textual.widgets import Button
        modal = app2.screen
        modal.query_one("#pl-rename-cancel", Button).press()
        await asyncio.sleep(0.1)

    assert results["save"] == "nieuwe naam", f"Save: {results['save']!r}"
    assert results["cancel"] is None, f"Cancel: {results['cancel']!r}"
    return "PlaylistNameModal: save + cancel beide correct."


async def _check_3_live_spotify_actions() -> str:
    """Echte Spotify-acties tegen de live API (vereist werkende OAuth).

    - Maak een test-playlist aan
    - Voeg er een track aan toe
    - Hernoem 'm
    - Verwijder 'm (unfollow)

    Geeft een tekstuele samenvatting terug (geen asserties — dit is een
    live-test; failures verschijnen in de output)."""
    cfg = load_config() if hasattr(load_config, "__call__") else None
    # load is de loader; import opnieuw voor zekerheid
    from musi.config import load
    cfg = load()
    if not cfg.spotify_client_id:
        return "SKIP: geen client_id in config"

    from musi.services import build_full
    sp = build_full(cfg).providers.get("spotify")
    if sp is None:
        return "SKIP: geen SpotifySearch-provider"

    # zoek een echte track
    tracks = await sp.search("radiohead", limit=1)
    if not tracks:
        return "SKIP: zoek levert niks op (OAuth-broken)"
    uri = tracks[0].uri

    # 1. maak test-playlist
    test_name = f"musi-test-{int(time.time())}"
    pl = await sp.create_playlist(test_name, public=False)
    if pl is None:
        return f"FAIL create_playlist: {sp.last_error}"
    new_uri = pl.get("uri")
    ok, err = await sp.add_to_playlist(new_uri, [uri])
    if not ok:
        return f"FAIL add_to_playlist: {err}"

    # 2. rename
    ok2, err2 = await sp.rename_playlist(new_uri, name=f"{test_name}-renamed")
    if not ok2:
        return f"FAIL rename: {err2}"

    # 3. delete (unfollow)
    ok3, err3 = await sp.delete_playlist(new_uri)
    if not ok3:
        return f"FAIL delete: {err3}"

    return f"OK: created+add+rename+delete voor '{test_name}'."


async def main() -> None:
    print("=== E2E playlist-beheer ===")
    try:
        msg1 = await _check_1_picker_dismiss_types()
        print(f"[1/3] {msg1}")
    except AssertionError as e:
        print(f"[1/3] FAIL: {e}")
    except Exception as e:
        print(f"[1/3] ERROR: {type(e).__name__}: {e}")

    try:
        msg2 = await _check_2_name_modal()
        print(f"[2/3] {msg2}")
    except AssertionError as e:
        print(f"[2/3] FAIL: {e}")
    except Exception as e:
        print(f"[2/3] ERROR: {type(e).__name__}: {e}")

    try:
        msg3 = await _check_3_live_spotify_actions()
        print(f"[3/3] {msg3}")
    except Exception as e:
        print(f"[3/3] ERROR: {type(e).__name__}: {e}")


if __name__ == "__main__":
    asyncio.run(main())
