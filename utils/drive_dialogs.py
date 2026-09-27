"""
Get from / Save to Sunet Drive (SUNET/scribe-backend#64): the dialogs.

Three pieces, each awaitable, composed by the two entry points at the
bottom:

- `ensure_connected()` -- the step where the reader visibly leaves Scribe:
  their Drive opens in a tab of its own, they sign in and grant access
  there, and this dialog waits for the grant. Drive is never made to look
  like storage inside Scribe; the issue is explicit about that boundary.
- `DriveBrowser` -- a folder browser over the reader's Drive, as they see
  it there. Nextcloud has no picker an outside site can embed, so this is
  Scribe's own, listing folders through the backend (WebDAV PROPFIND as the
  reader). It picks either a file to bring in or a folder to save into.
- `get_from_drive()` / `save_to_drive()` -- what the Upload row and the
  export dialog call.

The link that opens Drive is a real `<a target="_blank">` the reader
clicks, not a `window.open` from the server: the login URL arrives after a
round trip, and a window opened then is no longer part of the click, so a
popup blocker would stop it.

Names from Drive are the reader's own files' names -- drawn with labels,
never HTML.
"""

import asyncio
import posixpath

from typing import Awaitable, Callable, Optional

from nicegui import ui

from utils.drive import (
    DriveResult,
    display_name,
    drive_connect,
    drive_import,
    drive_list,
    drive_poll,
    drive_save,
    drive_status,
)

POLL_SECONDS = 2.0
# Nextcloud's sign-in token lasts 20 minutes; waiting longer is pointless.
POLL_GIVE_UP_SECONDS = 20 * 60


def _notify_error(result: DriveResult) -> None:
    ui.notify(result.error, type="negative", timeout=None, close_button="Close")


def format_size(size: Optional[int]) -> str:
    if size is None:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


def breadcrumbs(path: str) -> list[tuple[str, str]]:
    """
    (label, path) pairs from the top of the Drive down to `path`. The top
    is labelled by the caller.
    """

    crumbs = [("", "")]
    parts = [p for p in path.split("/") if p]
    for i, part in enumerate(parts):
        crumbs.append((part, "/".join(parts[: i + 1])))
    return crumbs


def unique_name(name: str, taken: set[str]) -> str:
    """
    `name`, or `name (2)`, `name (3)`... -- the first not in `taken`.
    """

    if name not in taken:
        return name

    stem, ext = posixpath.splitext(name)
    n = 2
    while f"{stem} ({n}){ext}" in taken:
        n += 1
    return f"{stem} ({n}){ext}"


# ---------------------------------------------------------------------------
# Connecting
# ---------------------------------------------------------------------------


async def ensure_connected(status: dict) -> bool:
    """
    Make sure the reader is connected to their Drive, asking them to
    connect when they are not.

    Parameters:
        status (dict): drive_status()'s result.

    Returns:
        bool: True when connected.
    """

    name = display_name(status)

    if status.get("connected"):
        return True

    if not status.get("instance"):
        with ui.dialog().props(f'aria-label="Choose your {name}"') as dialog, ui.card():
            ui.label(f"Which {name}?").classes("text-h6")
            ui.label(
                f"Your organisation has not said which {name} to use. "
                "Enter your own under User settings, e.g. https://su.drive.sunet.se."
            ).classes("text-body2")
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Close", on_click=dialog.close).props("flat color=black")
                ui.button(
                    "User settings", on_click=lambda: ui.navigate.to("/user")
                ).props("flat color=white").classes("button-default-style")
        dialog.on("hide", dialog.delete)
        dialog.open()
        return False

    started = await drive_connect()
    if not started.ok:
        _notify_error(started)
        return False

    login_url = started.result["login_url"]
    connected = asyncio.get_running_loop().create_future()

    with ui.dialog().props(
        f'persistent aria-label="Connect to {name}"'
    ) as dialog, ui.card().style("max-width: 480px;"):
        ui.label(f"Connect to {name}").classes("text-h6")
        ui.label(
            f"{name} opens in a new tab. Sign in there if asked, and grant "
            "Scribe access. Then come back to this tab."
        ).classes("text-body2")
        ui.label(
            f"Scribe will see the files you can see in {name}, and only while "
            "you use it: access ends after an hour without use, or when you "
            "disconnect under User settings."
        ).classes("text-body2 text-theme-muted")

        ui.link(f"Open {name}", login_url, new_tab=True).classes(
            "button-default-style q-btn q-btn--flat q-px-md q-py-sm"
        ).props('rel="noopener noreferrer"')

        with ui.row().classes("items-center gap-2"):
            ui.spinner(size="sm").props("aria-hidden=true")
            waiting = ui.label("Waiting for you to grant access...").props(
                "role=status aria-live=polite"
            )

        with ui.row().classes("w-full justify-end"):
            ui.button("Cancel", on_click=lambda: finish(False)).props(
                "flat color=black"
            )

    elapsed = 0.0

    def finish(value: bool) -> None:
        timer.deactivate()
        if not connected.done():
            connected.set_result(value)
        dialog.close()

    async def poll() -> None:
        nonlocal elapsed
        elapsed += POLL_SECONDS

        result = await drive_poll()

        if connected.done():
            return
        if not result.ok:
            waiting.set_text(result.error)
            return
        if result.result.get("state") == "connected":
            ui.notify(f"Connected to {name}", type="positive")
            finish(True)
        elif result.result.get("state") == "none" or elapsed > POLL_GIVE_UP_SECONDS:
            waiting.set_text("The sign-in has expired. Close this and try again.")
            timer.deactivate()

    timer = ui.timer(POLL_SECONDS, poll)
    dialog.on("hide", dialog.delete)
    dialog.open()

    return await connected


# ---------------------------------------------------------------------------
# Browsing
# ---------------------------------------------------------------------------


class DriveBrowser:
    """
    A folder browser over the reader's Drive.

    mode "file": pick one audio or video file; submits its path.
    mode "folder": pick a folder (and, with `filename`, a name) to save
        into; submits (folder, name).

    Await `open()`; None means cancelled.
    """

    def __init__(
        self,
        name: str,
        mode: str,
        filename: Optional[str] = None,
        count: int = 1,
    ) -> None:
        self.name = name
        self.mode = mode
        self.filename = filename
        self.count = count
        self.path = ""
        self.selected: Optional[str] = None
        self.entries: list[dict] = []

    async def open(self):
        title = (
            f"Get from {self.name}" if self.mode == "file" else f"Save to {self.name}"
        )

        with ui.dialog().props(f'aria-label="{title}"') as self.dialog, ui.card().style(
            "width: 640px; max-width: 95vw; min-width: 0;"
        ):
            ui.label(title).classes("text-h6")
            self.crumbs = ui.row().classes("items-center gap-1 w-full").props(
                f'role=navigation aria-label="Folder in {self.name}"'
            )
            self.status = ui.label("").classes("text-body2 text-theme-muted").props(
                "role=status aria-live=polite"
            )
            with ui.scroll_area().style("height: 360px; width: 100%;"):
                self.listing = ui.list().props("separator").classes("w-full")

            with ui.column().classes("w-full gap-2"):
                if self.mode == "folder" and self.filename is not None:
                    self.name_input = (
                        ui.input("File name", value=self.filename)
                        .props("outlined dense")
                        .classes("w-full")
                    )
                elif self.mode == "folder":
                    ui.label(
                        f"{self.count} files will be saved in the folder shown above."
                    ).classes("text-body2")

                with ui.row().classes("w-full justify-end gap-2"):
                    ui.button("Cancel", on_click=lambda: self.dialog.submit(None)).props(
                        "flat color=black"
                    )
                    self.confirm = (
                        ui.button(
                            "Get file" if self.mode == "file" else "Save here",
                            icon="cloud_download" if self.mode == "file" else "cloud_upload",
                            on_click=self.submit,
                        )
                        .props("flat color=white")
                        .classes("button-default-style")
                    )

        self.dialog.on("hide", self.dialog.delete)
        self.dialog.open()
        await self.go(self.path)
        return await self.dialog

    def submit(self) -> None:
        if self.mode == "file":
            if self.selected:
                self.dialog.submit(self.selected)
            return

        name = None
        if self.filename is not None:
            name = (self.name_input.value or "").strip()
            if not name or "/" in name or "\\" in name:
                self.name_input.props('error error-message="Enter a file name."')
                self.name_input.run_method("focus")
                return
            self.name_input.props(remove="error error-message")

        self.dialog.submit((self.path, name))

    def update_confirm(self) -> None:
        if self.mode == "file":
            self.confirm.set_enabled(bool(self.selected))
        else:
            self.confirm.set_enabled(True)

    async def go(self, path: str) -> None:
        self.status.set_text("Loading...")
        self.confirm.set_enabled(False)

        result = await drive_list(path)

        if not result.ok:
            self.status.set_text(result.error)
            if result.reason == "not_connected":
                self.dialog.submit(None)
                _notify_error(result)
            return

        self.path = result.result["path"]
        self.entries = result.result["entries"]
        self.selected = None
        self.draw()

    def draw(self) -> None:
        self.crumbs.clear()
        with self.crumbs:
            for i, (label, path) in enumerate(breadcrumbs(self.path)):
                if i:
                    ui.icon("chevron_right", size="xs").props("aria-hidden=true")
                ui.button(
                    label or self.name,
                    on_click=lambda _, p=path: self.go(p),
                ).props("flat dense no-caps color=black")

        self.listing.clear()
        shown = 0

        with self.listing:
            for entry in self.entries:
                if self.mode == "folder" and not entry["is_dir"]:
                    continue
                shown += 1
                self.draw_entry(entry)

        if not shown:
            self.status.set_text(
                "No audio or video files here."
                if self.mode == "file"
                else "No folders here. Save into this one, or go back."
            )
        else:
            self.status.set_text("")

        self.update_confirm()

    def draw_entry(self, entry: dict) -> None:
        is_dir = entry["is_dir"]
        usable = is_dir or (self.mode == "file" and entry.get("media"))
        selected = entry["path"] == self.selected

        async def clicked(_=None, e=entry) -> None:
            if e["is_dir"]:
                await self.go(e["path"])
            elif usable:
                self.selected = e["path"]
                self.draw()

        item = ui.item(on_click=clicked if usable else None).classes("w-full")
        if not usable:
            item.props("disable")
        if selected:
            item.props('active aria-selected="true"')
        item.props(
            f'aria-label="{"Folder" if is_dir else "File"}: {entry["name"]}"'
        )

        with item:
            with ui.item_section().props("avatar"):
                ui.icon(
                    "folder" if is_dir else ("movie" if entry.get("media") else "description")
                ).props("aria-hidden=true")
            with ui.item_section():
                ui.item_label(entry["name"]).classes("ellipsis")
                if not is_dir and entry.get("size") is not None:
                    ui.item_label(format_size(entry["size"])).props("caption")


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


async def get_from_drive(on_imported: Callable[[], Awaitable[None]]) -> None:
    """
    Bring a file in from the reader's Drive as a new job: connect if
    needed, browse, then transfer server to server.
    """

    status = await drive_status()
    if not status.ok:
        _notify_error(status)
        return
    if not status.result.get("enabled"):
        return

    name = display_name(status.result)

    if not await ensure_connected(status.result):
        return

    path = await DriveBrowser(name, "file").open()
    if not path:
        return

    filename = posixpath.basename(path)

    with ui.dialog().props(
        f'persistent aria-label="Getting file from {name}"'
    ) as progress, ui.card().classes("items-center"):
        ui.label(f"Getting {filename} from {name}...").classes("text-h6").props(
            "role=status aria-live=polite"
        )
        ui.spinner(size="50px").props("aria-hidden=true")
    progress.open()

    try:
        result = await drive_import(path)
    finally:
        progress.close()
        progress.delete()

    if not result.ok:
        _notify_error(result)
        return

    ui.notify(f"{filename} is in My files", type="positive")
    await on_imported()


async def _confirm_replace(name: str, filename: str) -> Optional[str]:
    """
    "replace", "keep" (save under a new name) or None (skip).
    """

    with ui.dialog().props(f'aria-label="{filename} already exists"') as dialog, ui.card():
        ui.label(f"{filename} already exists in {name}").classes("text-h6")
        ui.label("Replace it, or save this one under a new name?").classes("text-body2")
        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Skip", on_click=lambda: dialog.submit(None)).props("flat color=black")
            ui.button("Keep both", on_click=lambda: dialog.submit("keep")).props(
                "flat color=black"
            )
            ui.button("Replace", on_click=lambda: dialog.submit("replace")).props(
                "flat color=white"
            ).classes("button-default-style")

    dialog.on("hide", dialog.delete)
    dialog.open()
    return await dialog


async def save_to_drive(files: list[tuple[str, bytes]]) -> None:
    """
    Save exported files to the reader's Drive: connect if needed, choose a
    folder (and, for one file, its name), then send each. Nothing in Scribe
    is removed by it.

    Parameters:
        files: (file name, content) pairs.
    """

    if not files:
        return

    status = await drive_status()
    if not status.ok:
        _notify_error(status)
        return
    if not status.result.get("enabled"):
        return

    name = display_name(status.result)

    if not await ensure_connected(status.result):
        return

    single = len(files) == 1
    chosen = await DriveBrowser(
        name,
        "folder",
        filename=files[0][0] if single else None,
        count=len(files),
    ).open()
    if not chosen:
        return

    folder, new_name = chosen
    if single:
        files = [(new_name, files[0][1])]

    existing: Optional[set[str]] = None
    saved = 0

    for filename, content in files:
        result = await drive_save(folder, filename, content)

        if not result.ok and result.reason == "exists":
            choice = await _confirm_replace(name, filename)
            if choice == "replace":
                result = await drive_save(folder, filename, content, overwrite=True)
            elif choice == "keep":
                if existing is None:
                    listing = await drive_list(folder)
                    existing = {
                        e["name"] for e in (listing.result or {}).get("entries", [])
                    }
                filename = unique_name(filename, existing)
                result = await drive_save(folder, filename, content)
            else:
                continue

        if not result.ok:
            _notify_error(result)
            if result.reason == "not_connected":
                return
            continue

        if existing is not None:
            existing.add(filename)
        saved += 1

    if saved:
        where = f"{name}/{folder}" if folder else name
        what = files[0][0] if single else f"{saved} files"
        ui.notify(f"Saved {what} to {where}", type="positive")
