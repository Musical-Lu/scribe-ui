"""
Get from / Save to Sunet Drive (SUNET/scribe-backend#64), frontend side:
the backend client in utils/drive.py and the pieces of the dialogs that can
be checked without a browser.
"""

import json
import pathlib

import httpx
import pytest

import utils.drive as drive
from utils.drive_dialogs import breadcrumbs, format_size, unique_name


@pytest.fixture()
def backend(monkeypatch):
    """
    The backend's /drive endpoints, answering from `routes`: (method, path)
    -> (status, body). Records every request made.
    """

    routes: dict = {}
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        status, body = routes[(request.method, request.url.path)]
        return httpx.Response(status, json=body)

    real = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(drive.httpx, "AsyncClient", client)
    monkeypatch.setattr(drive, "get_auth_header", lambda: {"Authorization": "Bearer t"})
    monkeypatch.setattr(drive.settings, "API_URL", "http://backend")

    return routes, seen


@pytest.mark.asyncio
async def test_a_success_carries_the_result(backend):
    routes, seen = backend
    routes[("GET", "/api/v1/drive")] = (
        200,
        {"result": {"enabled": True, "display_name": "SU Box", "connected": False}},
    )

    status = await drive.drive_status()

    assert status.ok
    assert status.result["display_name"] == "SU Box"
    assert seen[0].headers["Authorization"] == "Bearer t"


@pytest.mark.asyncio
async def test_a_refusal_carries_its_message_and_reason(backend):
    routes, _ = backend
    routes[("GET", "/api/v1/drive/files")] = (
        409,
        {"error": "Connect to Drive first.", "reason": "not_connected"},
    )

    listing = await drive.drive_list("Lectures")

    assert not listing.ok
    assert listing.reason == "not_connected"
    assert listing.error == "Connect to Drive first."


@pytest.mark.asyncio
async def test_an_unreachable_backend_is_an_answer_not_an_exception(monkeypatch):
    def failing(request):
        raise httpx.ConnectError("down")

    real = httpx.AsyncClient
    monkeypatch.setattr(
        drive.httpx,
        "AsyncClient",
        lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(failing)}),
    )
    monkeypatch.setattr(drive, "get_auth_header", lambda: {})

    result = await drive.drive_status()

    assert not result.ok
    assert result.reason == "unavailable"


@pytest.mark.asyncio
async def test_saving_sends_the_bytes_and_asks_not_to_overwrite(backend):
    routes, seen = backend
    routes[("PUT", "/api/v1/drive/files")] = (200, {"result": {"path": "Notes/a.srt"}})

    await drive.drive_save("Notes", "a.srt", b"1\n00:00")

    request = seen[0]
    assert request.content == b"1\n00:00"
    assert request.url.params["path"] == "Notes"
    assert request.url.params["name"] == "a.srt"
    assert request.url.params["overwrite"] == "false"


@pytest.mark.asyncio
async def test_logging_out_asks_the_backend_to_revoke(backend):
    routes, seen = backend
    routes[("DELETE", "/api/v1/drive/connect")] = (200, {"result": {"state": "none"}})

    result = await drive.drive_logout()

    assert result.ok
    assert seen[0].method == "DELETE"


def test_readers_do_not_choose_an_instance():
    # The instance is the organisation's: nothing in the client sets one,
    # and User settings offers only logging out.
    assert not hasattr(drive, "drive_set_instance")

    user_page = pathlib.Path("pages/user.py").read_text()
    assert "drive_set_instance" not in user_page
    assert "Log out from {name}" in user_page


def test_several_files_can_be_chosen_across_folders():
    from utils.drive_dialogs import MAX_FILES, DriveBrowser

    browser = DriveBrowser("SU Box", "file")
    browser.update_confirm = lambda: None
    browser.draw = lambda: None

    first = {"path": "A/one.mp3", "name": "one.mp3", "is_dir": False, "media": True}
    second = {"path": "B/two.mp4", "name": "two.mp4", "is_dir": False, "media": True}

    browser.toggle(first, True)
    browser.entries = [second]
    browser.select_all_here()

    assert list(browser.selected) == ["A/one.mp3", "B/two.mp4"]

    browser.toggle(first, False)
    assert list(browser.selected) == ["B/two.mp4"]
    assert MAX_FILES >= 2


def test_folders_and_other_files_are_not_selected_by_select_all():
    from utils.drive_dialogs import DriveBrowser

    browser = DriveBrowser("SU Box", "file")
    browser.draw = lambda: None
    browser.entries = [
        {"path": "A", "name": "A", "is_dir": True},
        {"path": "notes.docx", "name": "notes.docx", "is_dir": False, "media": False},
        {"path": "talk.mp3", "name": "talk.mp3", "is_dir": False, "media": True},
    ]

    browser.select_all_here()

    assert list(browser.selected) == ["talk.mp3"]


@pytest.mark.asyncio
async def test_an_original_is_saved_by_the_backend_with_the_password(backend, monkeypatch):
    routes, seen = backend
    routes[("POST", "/api/v1/drive/save-original")] = (
        200,
        {"result": {"path": "Notes/Seminar.webm"}},
    )

    class Storage:
        user = {"encryption_password": "sealed"}

    class FakeApp:
        storage = Storage()

    monkeypatch.setattr(drive, "app", FakeApp)
    monkeypatch.setattr(drive, "storage_decrypt", lambda v: "opened" if v == "sealed" else None)

    result = await drive.drive_save_original("job-9", "Notes", "Seminar.webm")

    assert result.ok
    assert json.loads(seen[0].content) == {
        "job_id": "job-9",
        "encryption_password": "opened",
        "path": "Notes",
        "name": "Seminar.webm",
        "overwrite": False,
    }


def test_the_originals_go_to_drive_when_the_export_asks_for_them():
    source = pathlib.Path("utils/srt_export.py").read_text()
    drive_branch = source[source.index("if to_drive:"):source.index("if is_bulk:\n                                zip_buffer")]

    # Only when "Download the original recording" is ticked.
    assert "include_originals.value" in drive_branch
    assert "save_to_drive(" in drive_branch
    assert "originals" in drive_branch
    # And the originals-only export can save them to Drive as well.
    originals_dialog = source[source.index("def show_originals_dialog"):source.index("class ExportMixin")]
    assert "save_to_drive([], originals)" in originals_dialog


def test_an_original_never_passes_through_the_frontend():
    source = pathlib.Path("utils/drive_dialogs.py").read_text()
    save = source[source.index("async def save_to_drive"):]

    # Sent by job id; the backend decrypts and streams it into Drive.
    assert "drive_save_original(" in save
    assert "ORIGINAL_PREFIX" not in save


def test_the_drive_is_named_as_the_organisation_names_it():
    assert drive.display_name({"display_name": "SU Box"}) == "SU Box"
    assert drive.display_name({"display_name": "  "}) == "Sunet Drive"
    assert drive.display_name(None) == "Sunet Drive"


def test_breadcrumbs_run_from_the_top_down():
    assert breadcrumbs("") == [("", "")]
    assert breadcrumbs("Lectures/2026") == [
        ("", ""),
        ("Lectures", "Lectures"),
        ("2026", "Lectures/2026"),
    ]


def test_keeping_both_finds_a_free_name():
    assert unique_name("a.srt", set()) == "a.srt"
    assert unique_name("a.srt", {"a.srt"}) == "a (2).srt"
    assert unique_name("a.srt", {"a.srt", "a (2).srt"}) == "a (3).srt"


def test_sizes_read_like_sizes():
    assert format_size(None) == ""
    assert format_size(512) == "512 B"
    assert format_size(5 * 1024 * 1024) == "5.0 MB"


def test_export_does_not_mistake_the_click_for_a_drive_save():
    # exp(to_drive=False) is async and takes a parameter; handed to on_click
    # directly, NiceGUI passes the click event as that parameter, which is
    # truthy -- every plain Export would have gone to Drive instead.
    source = pathlib.Path("utils/srt_export.py").read_text()

    assert 'on_click=lambda: exp())' in source
    assert "on_click=exp)" not in source
    assert "on_click=lambda: exp(to_drive=True)" in source


def test_the_fifth_action_takes_a_row_of_its_own_on_a_phone():
    styles = pathlib.Path("utils/styles.py").read_text()
    home = pathlib.Path("pages/home.py").read_text()

    assert ".jobs-actions .jobs-action-wide" in styles
    assert "jobs-action-wide" in home


def test_names_from_drive_are_never_drawn_as_html():
    source = pathlib.Path("utils/drive_dialogs.py").read_text()

    assert "ui.html" not in source
    assert "ui.markdown" not in source


def test_the_footer_says_how_many_and_how_much():
    from utils.drive_dialogs import selection_summary

    assert selection_summary({}) == ""
    assert selection_summary({"a": {"size": 1024}}) == "1 file selected · 1.0 KB"
    assert (
        selection_summary({"a": {"size": 1024 * 1024}, "b": {"size": 1024 * 1024}})
        == "2 files selected · 2.0 MB"
    )
    # A size Drive did not give is not guessed at.
    assert selection_summary({"a": {"size": 5}, "b": {"size": None}}) == "2 files selected"


def test_modified_dates_read_like_dates():
    from utils.drive_dialogs import format_date

    assert format_date("2026-02-02T08:00:00+00:00") == "2 Feb 2026"
    assert format_date(None) == ""
    assert format_date("not a date") == ""


def test_the_header_checkbox_clears_only_this_folder():
    from utils.drive_dialogs import DriveBrowser

    browser = DriveBrowser("SU Box", "file")
    browser.draw = lambda: None
    elsewhere = {"path": "B/two.mp4", "name": "two.mp4", "is_dir": False, "media": True}
    here = {"path": "A/one.mp3", "name": "one.mp3", "is_dir": False, "media": True}

    browser.toggle(elsewhere, True)
    browser.entries = [here]
    browser.select_all_here()
    browser.clear_here()

    assert list(browser.selected) == ["B/two.mp4"]


def test_the_tree_is_drawn_open_down_to_the_current_folder():
    from utils.drive_dialogs import breadcrumbs

    # draw_tree opens exactly these; everything else stays a closed branch.
    assert {p for _, p in breadcrumbs("Lectures/Spring 2026")} == {
        "",
        "Lectures",
        "Lectures/Spring 2026",
    }


def test_the_browser_rows_are_real_controls():
    source = pathlib.Path("utils/drive_dialogs.py").read_text()

    # A file row is a <label> around a native checkbox, a folder row a
    # <button>: never a clickable div.
    assert 'ui.element("label").classes("drive-row")' in source
    assert 'ui.element("button").classes("drive-row")' in source
    assert "type=checkbox" in source


def test_the_browser_gives_up_its_tree_on_a_phone():
    styles = pathlib.Path("utils/styles.py").read_text()
    block = styles[styles.index("drive_styles = "):]

    phone = block[block.index("@media (max-width: 700px)"):]
    assert ".drive-tree {\n            display: none;" in phone
    assert ".drive-row-up {\n            display: grid;" in phone


def test_the_upload_dialog_lists_exactly_what_it_accepts():
    from utils.common import UPLOAD_EXTENSIONS, UPLOAD_FORMATS_SHOWN

    shown = {f".{name.lower()}" for name in UPLOAD_FORMATS_SHOWN.split(", ")}

    # Everything listed is accepted; everything accepted is listed, bar the
    # two spellings of a format already listed.
    assert shown <= set(UPLOAD_EXTENSIONS)
    assert set(UPLOAD_EXTENSIONS) - shown == {".aif", ".mpeg"}


def test_the_upload_limit_is_for_all_the_files_together():
    from utils.common import UPLOAD_MAX_TOTAL_BYTES

    source = pathlib.Path("utils/common.py").read_text()

    # One request carries every selected file, so the proxy's 4 GB is a
    # total: the dialog says so, and checks it under the proxy's own limit.
    assert "4 GB in total." in source
    assert "4 GB each" not in source
    assert "max_total_size=UPLOAD_MAX_TOTAL_BYTES" in source
    assert UPLOAD_MAX_TOTAL_BYTES < 4 * 1024**3


def test_file_icons_come_from_the_type_drive_reports():
    from utils.drive_dialogs import file_icon

    assert file_icon("video/mp4") == "o_movie"
    assert file_icon("audio/mpeg") == "o_audiotrack"
    assert file_icon("image/png") == "o_image"
    assert file_icon("text/plain") == "o_description"
    assert file_icon("application/pdf") == "o_picture_as_pdf"
    # Drive not saying, or saying something no family covers.
    assert file_icon(None) == "o_insert_drive_file"
    assert file_icon("application/octet-stream") == "o_insert_drive_file"


def test_file_icons_never_look_at_the_name():
    import inspect

    from utils.drive_dialogs import DriveBrowser, file_icon

    # Only the MIME type goes in; no list of endings to keep up to date.
    assert list(inspect.signature(file_icon).parameters) == ["mime"]
    assert 'file_icon(entry.get("mime"))' in inspect.getsource(DriveBrowser.draw_row)
