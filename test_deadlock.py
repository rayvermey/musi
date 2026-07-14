"""Regressie-test voor de EditTagsModal/ConfirmModal-hang (musi, 13 jul 2026).

**De bug:** een action die ``push_screen(modal, wait_for_dismiss=True)`` (of
``push_screen_wait``) await, **deadlockt** als de modal pas op een button-press
dismissed wordt. Reden: Textual's ``_on_key`` await de action op de App
message-pump → die pump is geblokkeerd terwijl de action wacht op de dismiss-
Future → de button-press die ``dismiss()`` zou triggeren kan niet verwerkt
worden → de Future wordt nooit opgelost → oneindige wachttijd. Eerdere
``run_worker``-varianten hadden hetzelfde symptoom; een unit-test met een
modal die *meteen* in ``on_mount`` dismissed (``dismiss(True)``) bleek
vals-positief omdat die de button-press-stap omzeilt.

**De fix in ``musi/app/musi_app.py``:** push modals via
``push_screen(modal, callback=cb)`` (synchroon, geen await); de action keert
direct terug zodat de pump vrij blijft; de dismiss-waarde komt binnen via
``cb`` op de App-pump; zware I/O gaat vanuit de callback via
``asyncio.create_task``.

Deze test vergelijkt 4 varianten (bindings op App-niveau zodat ze sowieso
triggeren):
1. Action await push_screen(wait_for_dismiss=True) direct (geen worker) →
   Textual raise ``NoActiveWorker``; modal opent, maar geen result terug.
2. Action start worker die push_screen met wait_for_dismiss doet → DEADLOCK.
3. Action start worker die push_screen_wait doet (asyncio.shield) → DEADLOCK.
4. (refererende) Action start worker die push_screen (zonder wait) doet +
   callback → werkt.
"""
from __future__ import annotations

import asyncio
import sys
import time

from textual.app import App
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label


class _Modal(ModalScreen[bool]):
    def __init__(self, name: str) -> None:
        super().__init__()
        self._name = name

    def compose(self):
        yield Vertical(
            Label(f"modal {self._name}", id="lbl"),
            Button("Save", id="save", variant="primary"),
            Button("Cancel", id="cancel"),
            id="modal-body",
        )

    def on_mount(self) -> None:
        self.query_one("#save").focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "save")


class _VariantApp(App):
    """Action gedrag hangt af van variant — bindings zitten op de app."""

    variant: str = "v1"
    received: bool | None = None

    BINDINGS = [
        Binding("e", "open_modal", "Open"),
        Binding("q", "quit", "Quit"),
    ]

    async def _v1_direct(self):
        """Action await push_screen(wait_for_dismiss=True) direct."""
        return await self.push_screen(_Modal("v1"), wait_for_dismiss=True)

    async def _v2_worker_raw(self):
        """Action start worker die push_screen met wait_for_dismiss doet."""
        async def _do_push():
            return await self.push_screen(_Modal("v2"), wait_for_dismiss=True)

        worker = self.run_worker(_do_push(), exclusive=True, group="modal")
        return await worker.wait()

    async def _v3_worker_wait(self):
        """Action start worker die push_screen_wait doet (asyncio.shield)."""
        async def _do_push():
            return await self.push_screen_wait(_Modal("v3"))

        worker = self.run_worker(_do_push(), exclusive=True, group="modal")
        return await worker.wait()

    async def _v4_worker_nodismiss(self):
        """Action start worker die push_screen doet zonder wait_for_dismiss
        + checkt via callback of het scherm nog actief is."""
        future: asyncio.Future = asyncio.get_event_loop().create_future()

        async def _do_push():
            await self.push_screen(_Modal("v4"), callback=future.set_result)

        self.run_worker(_do_push(), exclusive=True, group="modal")
        # Action keert meteen terug — modal blijft op de stack, maar de
        # action kan niet wachten op de dismiss-waarde vanuit deze context.
        return True

    async def action_open_modal(self) -> None:
        if self.variant == "v1":
            try:
                result = await self._v1_direct()
                self.received = bool(result)
            except Exception as e:
                self.received = False
                self.log(f"v1 FAULT: {type(e).__name__}: {e}")
        elif self.variant == "v2":
            try:
                result = await self._v2_worker_raw()
                self.received = bool(result)
            except Exception as e:
                self.received = False
                self.log(f"v2 FAULT: {type(e).__name__}: {e}")
        elif self.variant == "v3":
            try:
                result = await self._v3_worker_wait()
                self.received = bool(result)
            except Exception as e:
                self.received = False
                self.log(f"v3 FAULT: {type(e).__name__}: {e}")
        elif self.variant == "v4":
            self.received = await self._v4_worker_nodismiss()


async def _run(variant: str) -> int:
    app = _VariantApp()
    app.variant = variant
    t0 = time.monotonic()
    try:
        async with app.run_test(size=(60, 20)) as pilot:
            await pilot.pause()
            print(f"  [{variant}] focus={app.focused}")
            print(f"  [{variant}] press('e')…")
            t1 = time.monotonic()
            try:
                await asyncio.wait_for(pilot.press("e"), timeout=3.0)
                print(f"  [{variant}] press('e') duurde {time.monotonic()-t1:.2f}s")
            except asyncio.TimeoutError:
                print(f"  [{variant}] press('e') TIMEOUT na 3s — deadlock!")
                return 2

            for _ in range(30):
                await pilot.pause(0.05)
                if type(app.screen).__name__ != "Screen":
                    break
            print(f"  [{variant}] top screen na press: {type(app.screen).__name__}")
            print(f"  [{variant}] received na press: {app.received}")

            if type(app.screen).__name__ == "Screen":
                # Modal ging niet open, niets te testen.
                return 6

            print(f"  [{variant}] press('enter') om Save te activeren…")
            t2 = time.monotonic()
            try:
                await asyncio.wait_for(pilot.press("enter"), timeout=3.0)
                print(f"  [{variant}] press('enter') duurde {time.monotonic()-t2:.2f}s")
            except asyncio.TimeoutError:
                print(f"  [{variant}] press('enter') TIMEOUT na 3s — deadlock!")
                return 3

            for _ in range(50):
                await pilot.pause(0.05)
                if app.received is not None:
                    break

            print(f"  [{variant}] received = {app.received}")
            print(f"  [{variant}] top screen = {type(app.screen).__name__}")
            print(f"  [{variant}] TOTAAL: {time.monotonic()-t0:.2f}s")
            return 0 if app.received is True else 4
    except Exception as e:
        print(f"  [{variant}] EXCEPTION: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return 5


async def main() -> int:
    results = {}
    for v in ("v1", "v2", "v3", "v4"):
        print(f"\n=== {v} ===")
        results[v] = await _run(v)
    print("\n=== SAMENVATTING ===")
    for v, rc in results.items():
        print(f"  {v}: {'OK' if rc == 0 else f'FAIL ({rc})'}")
    return 0 if all(rc == 0 for rc in results.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))