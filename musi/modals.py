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
from textual.widgets import Button, Input, Label


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
