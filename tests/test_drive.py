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
async def test_an_empty_instance_goes_back_to_the_organisations(backend):
    routes, seen = backend
    routes[("PUT", "/api/v1/drive/instance")] = (200, {"result": {"instance": None}})

    await drive.drive_set_instance("")

    assert json.loads(seen[0].content) == {"url": None}


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
