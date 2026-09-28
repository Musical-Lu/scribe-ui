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
  reader). It picks either files to bring in -- any number, across folders
  -- or a folder to save into.
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

from datetime import datetime

from typing import Awaitable, Callable, Optional

from nicegui import ui
from nicegui.elements.mixins.text_element import TextElement

from utils.drive import (
    DriveResult,
    display_name,
    drive_connect,
    drive_import,
    drive_list,
    drive_poll,
    drive_save,
    drive_save_original,
    drive_status,
)

POLL_SECONDS = 2.0
# Files one Get from Drive brings in. Each is its own transfer and its own
# job; past this, a folder is better uploaded in parts than waited on.
MAX_FILES = 20
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
            "log out of it under User settings."
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


def span(text: str) -> TextElement:
    """
    Text in a <span>. ui.label draws a <div>, which is not allowed inside
    the <button> and <label> rows of the browser.
    """

    return TextElement(tag="span", text=text)


def format_date(value: Optional[str]) -> str:
    """
    "2 Feb 2026" from the ISO timestamp the backend sends, or "".
    """

    if not value:
        return ""
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return ""
    return f"{moment.day} {moment:%b %Y}"


def selection_summary(selected: dict[str, dict]) -> str:
    """
    "3 files selected · 1.2 GB" -- the size only when every file has one.
    """

    count = len(selected)
    if not count:
        return ""

    text = "1 file selected" if count == 1 else f"{count} files selected"
    sizes = [e.get("size") for e in selected.values()]
    if all(size is not None for size in sizes):
        text += f" · {format_size(sum(sizes))}"
    return text


class DriveBrowser:
    """
    A browser over the reader's Drive: a folder tree beside a table of the
    current folder (name, size, modified), the one of four drawn designs
    chosen for it.

    mode "file": pick audio and video files -- as many as MAX_FILES, from
        any number of folders; the selection is kept while the reader moves
        between them. Submits the chosen paths, in the order chosen.
    mode "folder": pick a folder (and, with `filename`, a name) to save
        into; submits (folder, name). The table shows folders only.

    The tree grows as the reader browses: every folder listed so far is
    remembered, and the path down to the current one is drawn open. There
    is no separate call to fill it -- a folder's children are known once it
    has been opened, which going down to anything inside it requires.

    Rows are plain HTML, not Quasar items: a file row is a <label> around a
    native checkbox, so a click anywhere on it toggles the file exactly
    once and a screen reader meets a named checkbox; a folder row is a
    <button>. Await `open()`; None means cancelled.
    """

    def __init__(
        self,
        name: str,
        mode: str,
        filename: Optional[str] = None,
        count: int = 1,
        instance: Optional[str] = None,
    ) -> None:
        self.name = name
        self.mode = mode
        self.filename = filename
        self.count = count
        self.instance = (instance or "").removeprefix("https://")
        self.path = ""
        # path -> entry; a dict keeps the order they were chosen in.
        self.selected: dict[str, dict] = {}
        self.entries: list[dict] = []
        # folder path -> the folders directly inside it, as listed.
        self.children: dict[str, list[dict]] = {}

    async def open(self):
        title = (
            f"Import from {self.name}" if self.mode == "file" else f"Save to {self.name}"
        )

        with ui.dialog().props(f'aria-label="{title}"') as self.dialog, ui.card().classes(
            "drive-browser"
        ):
            with ui.element("div").classes("drive-browser-head w-full"):
                ui.label(title).classes("text-h6")
                if self.instance:
                    ui.label(self.instance).classes("text-sm text-theme-muted")

            with ui.element("div").classes("drive-browser-body w-full"):
                self.tree = ui.element("nav").classes("drive-tree").props(
                    f'aria-label="Folders in {self.name}"'
                )
                with ui.element("div").classes("drive-table"):
                    self.table_head = ui.element("div").classes("drive-row drive-row-head")
                    self.status = ui.label("").classes("drive-empty").props(
                        "role=status aria-live=polite"
                    )
                    self.rows = ui.element("div").classes("drive-table-rows")

            with ui.element("div").classes("drive-browser-foot w-full"):
                with ui.row().classes("items-center gap-2"):
                    if self.mode == "file":
                        self.summary = ui.label("").classes("text-body2").props(
                            "role=status aria-live=polite"
                        )
                        self.clear = ui.button(
                            "Clear", on_click=self.clear_selection
                        ).props("flat dense no-caps color=black")
                    elif self.filename is not None:
                        self.name_input = (
                            ui.input("File name", value=self.filename)
                            .props("outlined dense")
                            .style("min-width: 280px;")
                        )
                    else:
                        self.folder_label = ui.label("").classes("text-body2")

                with ui.row().classes("items-center gap-2"):
                    ui.button("Cancel", on_click=lambda: self.dialog.submit(None)).props(
                        "flat color=black"
                    ).classes("cancel-style")
                    self.confirm = (
                        ui.button(
                            "Import" if self.mode == "file" else "Save here",
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
                self.dialog.submit(list(self.selected))
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

    def selectable_here(self) -> list[dict]:
        return [e for e in self.entries if not e["is_dir"] and e.get("media")]

    def toggle(self, entry: dict, value: bool) -> None:
        if value and entry["path"] not in self.selected:
            if len(self.selected) >= MAX_FILES:
                ui.notify(f"At most {MAX_FILES} files at a time.", type="warning")
            else:
                self.selected[entry["path"]] = entry
        elif not value:
            self.selected.pop(entry["path"], None)
        self.draw()

    def select_all_here(self) -> None:
        for entry in self.selectable_here():
            if len(self.selected) >= MAX_FILES:
                ui.notify(f"At most {MAX_FILES} files at a time.", type="warning")
                break
            self.selected.setdefault(entry["path"], entry)
        self.draw()

    def clear_here(self) -> None:
        for entry in self.selectable_here():
            self.selected.pop(entry["path"], None)
        self.draw()

    def clear_selection(self) -> None:
        self.selected.clear()
        self.draw()

    def update_confirm(self) -> None:
        if self.mode != "file":
            self.confirm.set_enabled(True)
            if self.filename is None:
                where = self.path or self.name
                self.folder_label.set_text(
                    f"{self.count} files will be saved in {where}."
                )
            return

        count = len(self.selected)
        self.confirm.set_enabled(bool(count))
        self.confirm.set_text(f"Import {count} files" if count > 1 else "Import")
        self.summary.set_text(selection_summary(self.selected))
        self.clear.set_visibility(bool(count))

    async def go(self, path: str) -> None:
        self.status.set_text("Loading...")
        self.status.set_visibility(True)
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
        self.children[self.path] = [e for e in self.entries if e["is_dir"]]
        self.draw()

    # -- Drawing ------------------------------------------------------------

    def draw(self) -> None:
        self.draw_tree()
        self.draw_head()
        self.draw_rows()
        self.update_confirm()

    def draw_tree(self) -> None:
        # Every folder on the way down to the current one is drawn open.
        open_paths = {path for _, path in breadcrumbs(self.path)}

        def branch(path: str, label: str, depth: int) -> None:
            current = path == self.path
            button = ui.button(
                label,
                icon="o_folder_open" if path in open_paths else "o_folder",
                on_click=lambda _, p=path: self.go(p),
            ).props("flat dense no-caps color=black align=left")
            button.style(f"padding-left: {8 + depth * 16}px;")
            if current:
                button.classes("drive-tree-current").props('aria-current="true"')
            if path in open_paths:
                for child in self.children.get(path, []):
                    branch(child["path"], child["name"], depth + 1)

        self.tree.clear()
        with self.tree:
            branch("", self.name, 0)

    def draw_head(self) -> None:
        self.table_head.clear()
        with self.table_head:
            here = self.selectable_here()
            if self.mode == "file" and here:
                chosen = sum(1 for e in here if e["path"] in self.selected)
                box = ui.element("input").props(
                    'type=checkbox aria-label="Select all audio and video in this folder"'
                )
                if chosen == len(here):
                    box.props("checked")
                elif chosen:
                    box.props("indeterminate")
                box.on(
                    "change",
                    lambda: self.clear_here() if chosen == len(here) else self.select_all_here(),
                )
            else:
                ui.element("span")
            span("Name")
            span("Size").classes("drive-cell-num")
            span("Modified").classes("drive-cell-num")

    def draw_rows(self) -> None:
        self.rows.clear()
        shown = 0

        with self.rows:
            if self.path:
                parent = posixpath.dirname(self.path)
                with ui.element("button").classes("drive-row drive-row-up").props(
                    'type=button aria-label="Up one folder"'
                ).on("click", lambda: self.go(parent)):
                    ui.element("span")
                    with ui.element("span").classes("drive-cell-name"):
                        ui.icon("arrow_upward").props("aria-hidden=true")
                        span("..")
                    ui.element("span")
                    ui.element("span")

            for entry in self.entries:
                if self.mode == "folder" and not entry["is_dir"]:
                    continue
                shown += 1
                self.draw_row(entry)

        if shown:
            self.status.set_visibility(False)
        else:
            self.status.set_visibility(True)
            self.status.set_text(
                "No audio or video files here."
                if self.mode == "file"
                else "No folders here. Save into this one, or go back."
            )

    def draw_row(self, entry: dict) -> None:
        is_dir = entry["is_dir"]
        choosable = self.mode == "file" and not is_dir and entry.get("media")
        size = format_size(entry.get("size")) if entry.get("size") is not None else ""

        if is_dir:
            row = ui.element("button").classes("drive-row").props(
                f'type=button aria-label="Open folder {entry["name"]}"'
            )
            row.on("click", lambda _, p=entry["path"]: self.go(p))
        elif choosable:
            row = ui.element("label").classes("drive-row")
            if entry["path"] in self.selected:
                row.classes("drive-row-selected")
        else:
            row = ui.element("div").classes("drive-row drive-row-disabled")

        with row:
            if choosable:
                box = ui.element("input").props(
                    f'type=checkbox aria-label="{entry["name"]}"'
                )
                selected = entry["path"] in self.selected
                if selected:
                    box.props("checked")
                box.on(
                    "change",
                    lambda _, e=entry, now=selected: self.toggle(e, not now),
                )
            else:
                ui.element("span")

            with ui.element("span").classes("drive-cell-name"):
                ui.icon(
                    "o_folder"
                    if is_dir
                    else ("o_movie" if entry.get("media") else "o_description")
                ).props("aria-hidden=true")
                span(entry["name"])
            span(size).classes("drive-cell-num")
            span(format_date(entry.get("modified"))).classes("drive-cell-num")


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


async def get_from_drive(on_imported: Callable[[], Awaitable[None]]) -> None:
    """
    Bring files in from the reader's Drive, each as a new job: connect if
    needed, browse and choose, then transfer server to server -- one file
    at a time, so the backend streams one at a time and the reader sees
    how far it has got. A file that fails does not stop the rest, except a
    lost Drive connection, which stops them all.
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

    paths = await DriveBrowser(
        name, "file", instance=status.result.get("instance")
    ).open()
    if not paths:
        return

    stopping = False

    def stop() -> None:
        nonlocal stopping
        stopping = True
        stop_button.set_enabled(False)
        stop_button.set_text("Stopping after this file...")

    with ui.dialog().props(
        f'persistent aria-label="Importing files from {name}"'
    ) as progress, ui.card().classes("items-center"):
        heading = ui.label("").classes("text-h6").props(
            "role=status aria-live=polite"
        )
        ui.spinner(size="50px").props("aria-hidden=true")
        stop_button = ui.button("Stop after this file", on_click=stop).props(
            "flat color=black"
        )
        stop_button.set_visibility(len(paths) > 1)
    progress.open()

    done: list[str] = []
    failed: list[tuple[str, str]] = []

    try:
        for n, path in enumerate(paths, start=1):
            if stopping:
                break

            filename = posixpath.basename(path)
            heading.set_text(
                f"Importing {filename} from {name}..."
                if len(paths) == 1
                else f"Importing {n} of {len(paths)} from {name}: {filename}"
            )

            result = await drive_import(path)

            if result.ok:
                done.append(filename)
                await on_imported()
                continue

            failed.append((filename, result.error))
            if result.reason == "not_connected":
                break
    finally:
        progress.close()
        progress.delete()

    if done:
        ui.notify(
            f"{done[0]} is in My files"
            if len(done) == 1
            else f"{len(done)} files are in My files",
            type="positive",
        )
    for filename, error in failed:
        ui.notify(
            f"{filename}: {error}", type="negative", timeout=None, close_button="Close"
        )
    skipped = len(paths) - len(done) - len(failed)
    if skipped:
        ui.notify(f"{skipped} files were not fetched.", type="info")


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


async def save_to_drive(
    files: list[tuple[str, bytes]],
    originals: Optional[list[tuple[str, str]]] = None,
) -> None:
    """
    Save exported files -- and recordings' originals, when the export asked
    for them -- to the reader's Drive: connect if needed, choose a folder
    (and, for a lone file, its name), then send each. Nothing in Scribe is
    removed by it.

    An original is not sent from here: the backend decrypts it and streams
    it into Drive itself (drive_save_original), so a recording tens of
    megabytes long never passes through this process.

    Parameters:
        files: (file name, content) pairs.
        originals: (file name, job uuid) pairs.
    """

    originals = originals or []

    if not files and not originals:
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

    # Only a lone exported file gets a name box; with anything else going
    # too, each keeps its own name.
    single = len(files) == 1 and not originals
    total = len(files) + len(originals)
    chosen = await DriveBrowser(
        name,
        "folder",
        filename=files[0][0] if single else None,
        count=total,
        instance=status.result.get("instance"),
    ).open()
    if not chosen:
        return

    folder, new_name = chosen
    if single:
        files = [(new_name, files[0][1])]

    # Each item: its name, and how to send it under a given name.
    def send_file(content: bytes):
        return lambda filename, overwrite: drive_save(
            folder, filename, content, overwrite=overwrite
        )

    def send_original(uuid: str):
        return lambda filename, overwrite: drive_save_original(
            uuid, folder, filename, overwrite=overwrite
        )

    items = [(filename, send_file(content)) for filename, content in files]
    items += [(filename, send_original(uuid)) for filename, uuid in originals]

    with ui.dialog().props(
        f'persistent aria-label="Saving to {name}"'
    ) as progress, ui.card().classes("items-center"):
        heading = ui.label("").classes("text-h6").props(
            "role=status aria-live=polite"
        )
        ui.spinner(size="50px").props("aria-hidden=true")

    existing: Optional[set[str]] = None
    saved: list[str] = []

    try:
        for n, (filename, send) in enumerate(items, start=1):
            heading.set_text(
                f"Saving {filename} to {name}..."
                if total == 1
                else f"Saving {n} of {total} to {name}: {filename}"
            )
            progress.open()

            result = await send(filename, False)

            if not result.ok and result.reason == "exists":
                progress.close()
                choice = await _confirm_replace(name, filename)
                progress.open()
                if choice == "replace":
                    result = await send(filename, True)
                elif choice == "keep":
                    if existing is None:
                        listing = await drive_list(folder)
                        existing = {
                            e["name"]
                            for e in (listing.result or {}).get("entries", [])
                        }
                    filename = unique_name(filename, existing)
                    result = await send(filename, False)
                else:
                    continue

            if not result.ok:
                _notify_error(result)
                if result.reason == "not_connected":
                    return
                continue

            if existing is not None:
                existing.add(filename)
            saved.append(filename)
    finally:
        progress.close()
        progress.delete()

    if saved:
        where = f"{name}/{folder}" if folder else name
        what = saved[0] if len(saved) == 1 else f"{len(saved)} files"
        ui.notify(f"Saved {what} to {where}", type="positive")
