"""Modals — herbruikbare pop-up-schermen.

Twee schermen, allebei ``ModalScreen``-subclasses:

* **``ConfirmModal``** — generieke "weet je het zeker?"-bevestiging. Toont een
  titel + bericht; ``j``/``enter`` = bevestig, ``n``/``esc`` = annuleer.
  Dismissed met ``True``/``False``.

* **``EditTagsModal``** — editor voor de ID3-tags van één lokale track. Toont
  4 ``Input``-velden (Titel/Artiest/Album/Genre) vooringevuld met de huidige
  waarden + Opslaan/Annuleren-knoppen; ``esc`` = annuleren. Dismissed met een
  ``dict`` ``{"title","artist","album","genre"}`` bij opslaan, anders ``None``.

Caller-hangt ze via::

    result = await self.app.push_screen(modal, wait_for_dismiss=True)

en krijgt de dismiss-waarde terug (``True``/``False``/``dict``/``None``).

Let op (Textual 8.x, zie musi-project-memory): ``Input.bind`` bestaat niet, en
een letter als ``s`` met ``priority=True`` zou typen in een Input blokkeren —
vandaar Buttons i.p.v. key-bindings voor opslaan in de edit-modal.
"""
from __future__ import annotations

import os

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label


class ConfirmModal(ModalScreen[bool]):
    """Generieke ja/nee-bevestiging.

    Dismissed met ``True`` (ja) of ``False`` (nee). ``priority=True`` op de
    bindings zodat ze ook werken als er toevallig een widget focus heeft.
    """

    BINDINGS = [
        Binding("j,enter", "yes", "Ja", priority=True),
        Binding("n,escape", "no", "Nee", priority=True),
    ]

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-modal"):
            yield Label(self._title, id="confirm-title")
            yield Label(self._body, id="confirm-body")
            yield Label("[j] Ja    [n] Nee", id="confirm-hint")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)

    DEFAULT_CSS = """
    ConfirmModal {
        align: center middle;
    }
    #confirm-modal {
        background: $panel;
        border: thick $accent;
        width: 64;
        height: auto;
        padding: 1 2;
    }
    #confirm-title {
        text-style: bold;
        color: $text;
        margin-bottom: 1;
    }
    #confirm-body {
        color: $text-muted;
        margin-bottom: 1;
    }
    #confirm-hint {
        color: $accent;
    }
    """


class EditTagsModal(ModalScreen[dict | None]):
    """Editor voor ID3-tags van één lokale track (titel/artiest/album/genre).

    Dismissed met een ``{"title","artist","album","genre"}``-dict bij opslaan,
    of ``None`` bij annuleren. Opslaan via de knop (géén letter-binding — die
    zou typen in de invoervelden blokkeren); ``esc`` annuleert.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Annuleren", priority=True),
    ]

    def __init__(self, path: str, *, title: str, artist: str,
                 album: str, genre: str) -> None:
        super().__init__()
        self._path = path
        self._init = {"title": title, "artist": artist,
                      "album": album, "genre": genre}

    def compose(self) -> ComposeResult:
        name = os.path.basename(self._path)
        with Vertical(id="edit-modal"):
            yield Label(f"Tags wijzigen — {name}", id="edit-title")
            yield Label("Titel", classes="edit-lbl")
            yield Input(value=self._init["title"] or "", id="edit-title-input")
            yield Label("Artiest", classes="edit-lbl")
            yield Input(value=self._init["artist"] or "", id="edit-artist-input")
            yield Label("Album", classes="edit-lbl")
            yield Input(value=self._init["album"] or "", id="edit-album-input")
            yield Label("Genre", classes="edit-lbl")
            yield Input(value=self._init["genre"] or "", id="edit-genre-input")
            with Horizontal(id="edit-buttons"):
                yield Button("Opslaan", id="edit-save", variant="primary")
                yield Button("Annuleren", id="edit-cancel")

    async def on_mount(self) -> None:
        # Focus het titel-veld zodat je meteen kunt typen. Textual roept
        # ``on_mount`` aan nadat de kinderen gemount zijn, dus de query is
        # meteen vindbaar (in tegenstelling tot ``__init__`` of ``compose``).
        self.query_one("#edit-title-input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "edit-save":
            self.dismiss({
                "title": self.query_one("#edit-title-input", Input).value,
                "artist": self.query_one("#edit-artist-input", Input).value,
                "album": self.query_one("#edit-album-input", Input).value,
                "genre": self.query_one("#edit-genre-input", Input).value,
            })
        elif event.button.id == "edit-cancel":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    DEFAULT_CSS = """
    EditTagsModal {
        align: center middle;
    }
    #edit-modal {
        background: $panel;
        border: thick $accent;
        width: 72;
        height: auto;
        padding: 1 2;
    }
    #edit-title {
        text-style: bold;
        margin-bottom: 1;
    }
    .edit-lbl {
        color: $text-muted;
        margin-top: 0;
    }
    #edit-buttons {
        height: auto;
        margin-top: 1;
        align-horizontal: right;
    }
    #edit-buttons Button {
        margin-left: 1;
    }
    """


# Playlist-picker: geeft ``("playlist", uri) | ("liked", None) | ("new", name) | None``
PlaylistPick = tuple | None


class PlaylistPickerModal(ModalScreen[PlaylistPick]):
    """Doel-picker voor "track opslaan in playlist of Liked Songs".

    Lijst met:
      * ``♡ Liked Songs`` (rij-key ``__liked__``)
      * Per eigen/gevolgde playlist (rij-key ``pl:<uri>``; toont naam,
        ``tracks.total`` en een 🔒/🌐-marker)
      * ``+ Nieuwe playlist…`` (rij-key ``__new__``; bij Enter wordt de huidige
        filter-tekst als naam gebruikt — een lege filter + Enter op deze rij
        laat de modal niks doen en geeft een app-notify via de caller)

    Dismissed met:
      * ``("playlist", uri)`` — kies de playlist
      * ``("liked", None)`` — Liked Songs
      * ``("new", name)`` — nieuwe playlist aanmaken met deze naam
      * ``None`` — geannuleerd (Escape)

    Het filter-Input maakt subset-selectie mogelijk; de DataTable blijft
    scrollen over de hele lijst. ``enter`` is ``priority=True`` zodat de
    DataTable's eigen Enter-binding ('Select cells under the cursor') 'm niet
    opvangt vóór de picker-actie.

    De caller geeft de SpotifySearch-provider mee zodat de modal de lijst
    playlists + Liked Songs-count zelf ophaalt (in een thread via
    ``asyncio.to_thread`` vanuit een worker — hier direkt via
    ``call_from_thread`` niet nodig omdat de data-laag async is).
    """

    BINDINGS = [
        Binding("enter", "pick", "Kies", priority=True),
        Binding("escape", "cancel", "Annuleer", priority=True),
    ]

    def __init__(self, spotify, track_label: str) -> None:
        super().__init__()
        self._sp = spotify
        self._track_label = track_label
        self._filter: str = ""
        # cache: alle playlists (volledige dicts) + gefilterde weergave
        self._playlists: list[dict] = []
        self._liked_count: int = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="pl-pick-modal"):
            yield Label("Opslaan in playlist", id="pl-pick-title")
            yield Label(self._track_label, id="pl-pick-subtitle")
            yield Input(placeholder="Filter…", id="pl-pick-filter")
            yield DataTable(id="pl-pick-table", cursor_type="row")
            yield Label(
                "[Enter] kies  ·  [Esc] annuleer  ·  typ om te filteren",
                id="pl-pick-hint",
            )

    async def on_mount(self) -> None:
        # kolommen + eerste focus op de Input (leeg filter = alles zichtbaar)
        self.query_one("#pl-pick-table", DataTable).add_columns(
            "✓", "Naam", "Tracks")
        self.query_one("#pl-pick-filter", Input).focus()
        # haal playlists + Liked Songs-count op de achtergrond
        self.run_worker(self._load_lists(), exclusive=True)

    async def _load_lists(self) -> None:
        sp = self._sp
        if sp is None:
            self._render_table()
            return
        # beide ophalen in parallel — onafhankelijk van elkaar
        import asyncio
        playlists, liked = await asyncio.gather(
            sp.user_playlists(limit=200),
            sp.saved_track_count(),
        )
        self._playlists = list(playlists or [])
        self._liked_count = int(liked or 0)
        self._render_table()

    def _render_table(self) -> None:
        """Herbouw de tabel op basis van ``self._filter`` (case-insensitive
        substring op naam). Behoudt cursor-positie bij gelijke cursor-key."""
        tbl = self.query_one("#pl-pick-table", DataTable)
        # bewaar huidige cursor-key (indien aanwezig)
        try:
            cur_key = tbl.coordinate_to_cell_key(
                tbl.cursor_coordinate).row_key.value
        except Exception:
            cur_key = None
        tbl.clear()
        f = (self._filter or "").strip().lower()
        # 1) Liked Songs — toon als eerste, met count
        tbl.add_row("", "♡ Liked Songs", str(self._liked_count), key="__liked__")
        # 2) Gefilterde playlists
        matched = 0
        for p in self._playlists:
            name = (p.get("name") or "?")
            if f and f not in name.lower():
                continue
            uri = p.get("uri") or ""
            count = (p.get("tracks") or {}).get("total")
            count_s = str(count) if count is not None else "—"
            vis = "🔒" if p.get("public") is False else "🌐"
            tbl.add_row(vis, name, count_s, key=f"pl:{uri}")
            matched += 1
        # 3) Nieuwe-playlist-actie
        new_label = (
            "+ Nieuwe playlist… (typ naam + Enter)"
            if not f
            else f"+ Nieuwe playlist: “{self._filter}”"
        )
        tbl.add_row("", new_label, "", key="__new__")
        # cursor terugzetten indien mogelijk
        if cur_key:
            for i in range(tbl.row_count):
                try:
                    if tbl.coordinate_to_cell_key(
                            (i, 0)).row_key.value == cur_key:
                        tbl.move_cursor(row=i)
                        return
                except Exception:
                    pass
        # anders: cursor op rij 0
        if tbl.row_count > 0:
            tbl.move_cursor(row=0)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "pl-pick-filter":
            self._filter = event.value
            self._render_table()

    def _selected_key(self) -> str | None:
        tbl = self.query_one("#pl-pick-table", DataTable)
        if tbl.row_count == 0:
            return None
        try:
            return tbl.coordinate_to_cell_key(
                tbl.cursor_coordinate).row_key.value
        except Exception:
            return None

    def action_pick(self) -> None:
        # priority-Enter binding. Cursor kan op filter-Input staan of op de
        # tabel — beide gevallen afhandelen: als de Input focus heeft, geven
        # we de filter-tekst door aan de eerste rij van de tabel (visueel:
        # cursor naar boven) en dispatchen we daar.
        filt = self.query_one("#pl-pick-filter", Input)
        if filt.has_focus:
            tbl = self.query_one("#pl-pick-table", DataTable)
            if tbl.row_count > 0:
                tbl.move_cursor(row=0)
                tbl.focus()
            return
        key = self._selected_key()
        if key is None:
            return
        if key == "__liked__":
            self.dismiss(("liked", None))
        elif key == "__new__":
            name = (self._filter or "").strip()
            if not name:
                # geen naam → focus de Input en wacht op de gebruiker
                self.query_one("#pl-pick-filter", Input).focus()
                return
            self.dismiss(("new", name))
        elif key.startswith("pl:"):
            self.dismiss(("playlist", key[3:]))
        # onbekende key: niets doen

    def action_cancel(self) -> None:
        self.dismiss(None)

    DEFAULT_CSS = """
    PlaylistPickerModal {
        align: center middle;
    }
    #pl-pick-modal {
        background: $panel;
        border: thick $accent;
        width: 78;
        height: auto;
        max-height: 24;
        padding: 1 2;
    }
    #pl-pick-title {
        text-style: bold;
        margin-bottom: 1;
    }
    #pl-pick-subtitle {
        color: $text-muted;
        margin-bottom: 1;
    }
    #pl-pick-filter {
        margin-bottom: 1;
    }
    #pl-pick-table {
        height: 1fr;
        min-height: 8;
    }
    #pl-pick-hint {
        color: $accent;
        margin-top: 1;
    }
    """


class PlaylistNameModal(ModalScreen[str | None]):
    """Één-Input modal voor het hernoemen van een playlist.

    Dismissed met de nieuwe naam (leading/trailing whitespace gestript) bij
    ``Opslaan`` of enter (mits niet leeg), of ``None`` bij ``Annuleren``/Esc.
    Net als ``EditTagsModal`` blijft de enter-binding uit de buurt — de
    Opslaan-knop is de trigger, zodat typen in het veld niet wordt
    onderschept.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Annuleren", priority=True),
    ]

    def __init__(self, title: str, current_name: str) -> None:
        super().__init__()
        self._title = title
        self._current = current_name or ""

    def compose(self) -> ComposeResult:
        with Vertical(id="pl-rename-modal"):
            yield Label(self._title, id="pl-rename-title")
            yield Label("Nieuwe naam", classes="pl-rename-lbl")
            yield Input(value=self._current, id="pl-rename-input")
            with Horizontal(id="pl-rename-buttons"):
                yield Button("Opslaan", id="pl-rename-save", variant="primary")
                yield Button("Annuleren", id="pl-rename-cancel")

    async def on_mount(self) -> None:
        # Focus het naam-veld en selecteer de tekst (zodat de gebruiker 'm
        # meteen kan overschrijven).
        inp = self.query_one("#pl-rename-input", Input)
        inp.focus()
        # selecteer alles
        try:
            inp.selection = (0, len(inp.value))
        except Exception:
            pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "pl-rename-save":
            name = self.query_one(
                "#pl-rename-input", Input).value.strip()
            if not name:
                return
            self.dismiss(name)
        elif event.button.id == "pl-rename-cancel":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    DEFAULT_CSS = """
    PlaylistNameModal {
        align: center middle;
    }
    #pl-rename-modal {
        background: $panel;
        border: thick $accent;
        width: 56;
        height: auto;
        padding: 1 2;
    }
    #pl-rename-title {
        text-style: bold;
        margin-bottom: 1;
    }
    .pl-rename-lbl {
        color: $text-muted;
        margin-bottom: 0;
    }
    #pl-rename-buttons {
        height: auto;
        margin-top: 1;
        align-horizontal: right;
    }
    #pl-rename-buttons Button {
        margin-left: 1;
    }
    """
