"""
One ZIP for every export of more than one file.

An export can mean several files: transcripts or subtitles for several jobs,
and -- with "Include the original recording" -- their originals. Those used
to arrive as one ZIP of transcripts plus one separate download per original,
since an original is tens of megabytes and the ZIP was built in this
process's memory. Now everything goes in one ZIP, written as it is sent:

- The export dialog registers what goes in (`prepare_zip()`): the files it
  has already built, and the originals by job id. It gets back a one-time
  link, `ZIP_PREFIX/<token>`, which the browser downloads.
- The route streams the archive. Built files go in first; then each
  original is fetched from the backend -- decrypted there with the session's
  encryption password, exactly as the single download (recording_api.py)
  is -- and piped into its ZIP entry chunk by chunk. Nothing is held whole
  and nothing touches the disk.
- An original that cannot be had is not a failed download: the rest of the
  ZIP still arrives, with a note in it naming what is missing.

A link is single-use, lives ZIP_TTL_SECONDS, and answers only the session
that made it -- the owner key recording_api derives from the session, never
anything sent back by the browser.
"""

import secrets
import time
import zipfile

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Optional
from urllib.parse import quote

import httpx

from fastapi.responses import Response, StreamingResponse
from nicegui import app

from utils.recording_api import JOB_PATTERN, _encryption_password, _signed_in_owner
from utils.settings import get_settings

settings = get_settings()

ZIP_PREFIX = "/record/export"
ZIP_TTL_SECONDS = 10 * 60

MISSING_NOTE = "NOT INCLUDED.txt"

# token -> {"owner", "expires", "name", "files", "originals"}
_pending: dict[str, dict] = {}


def _sweep() -> None:
    now = time.monotonic()
    for token in [t for t, entry in _pending.items() if entry["expires"] < now]:
        _pending.pop(token, None)


def unique_names(names: list[str]) -> list[str]:
    """
    The same names, made distinct: "a.srt", "a (2).srt", ... -- a ZIP may
    hold two entries with one name, and unzipping then keeps only one.
    """

    seen: set[str] = set()
    out = []

    for name in names:
        candidate = name
        stem, dot, ext = name.rpartition(".")
        if not dot:
            stem, ext = name, ""
        n = 2
        while candidate in seen:
            candidate = f"{stem} ({n}).{ext}" if ext else f"{stem} ({n})"
            n += 1
        seen.add(candidate)
        out.append(candidate)

    return out


def prepare_zip(
    name: str,
    files: list[tuple[str, bytes]],
    originals: Optional[list[tuple[str, str]]] = None,
) -> Optional[str]:
    """
    Register an export and return the link that downloads it as one ZIP,
    or None when nobody is signed in.

    Parameters:
        name: the ZIP's own file name.
        files: (file name, content) pairs, already built.
        originals: (file name, job uuid) pairs, fetched while the ZIP is
            sent.
    """

    owner = _signed_in_owner()
    if not owner:
        return None

    _sweep()

    token = secrets.token_urlsafe(24)
    _pending[token] = {
        "owner": owner,
        "expires": time.monotonic() + ZIP_TTL_SECONDS,
        "name": name,
        "files": list(files),
        "originals": [
            (filename, uuid)
            for filename, uuid in (originals or [])
            if JOB_PATTERN.match(uuid or "")
        ],
    }

    return f"{ZIP_PREFIX}/{token}"


class _Sink:
    """
    Where zipfile writes: bytes collect here until the stream takes them.
    Not seekable, so zipfile writes each entry's sizes after its data (a
    data descriptor) rather than seeking back to fill them in.
    """

    def __init__(self) -> None:
        self.buffer = bytearray()

    def write(self, data) -> int:
        self.buffer.extend(data)
        return len(data)

    def flush(self) -> None:
        pass

    def take(self) -> bytes:
        data = bytes(self.buffer)
        self.buffer.clear()
        return data


# (uuid) -> an async iterator of the original's bytes, or None when it
# cannot be had.
Fetch = Callable[[str], Awaitable[Optional[AsyncIterator[bytes]]]]


async def zip_stream(
    files: list[tuple[str, bytes]],
    originals: list[tuple[str, str]],
    fetch: Fetch,
) -> AsyncIterator[bytes]:
    """
    The ZIP, as it is written. Built files first, then the originals, each
    piped from `fetch` into its entry.
    """

    names = unique_names(
        [name for name, _ in files] + [name for name, _ in originals]
    )
    file_names, original_names = names[: len(files)], names[len(files) :]

    sink = _Sink()
    archive = zipfile.ZipFile(sink, "w", zipfile.ZIP_DEFLATED)
    now = time.localtime()[:6]
    missing = []

    for name, (_, content) in zip(file_names, files):
        archive.writestr(zipfile.ZipInfo(name, now), content, zipfile.ZIP_DEFLATED)
        yield sink.take()

    for name, (_, uuid) in zip(original_names, originals):
        chunks = await fetch(uuid)

        if chunks is None:
            missing.append(name)
            continue

        info = zipfile.ZipInfo(name, now)
        # Audio and video are compressed already; the lightest level spends
        # next to no time on data that will not shrink anyway.
        info.compress_type = zipfile.ZIP_DEFLATED
        info.compress_level = 1

        with archive.open(info, "w", force_zip64=True) as entry:
            async for piece in chunks:
                entry.write(piece)
                data = sink.take()
                if data:
                    yield data

        yield sink.take()

    if missing:
        archive.writestr(
            zipfile.ZipInfo(MISSING_NOTE, now),
            "These original recordings could not be included:\n\n"
            + "\n".join(missing)
            + "\n\nDownload them from My files, or try the export again.\n",
        )

    archive.close()
    yield sink.take()


def _backend_fetch(password: str, token: Optional[str]) -> Fetch:
    """
    Fetch originals from the backend as the session's own user, the same
    request the single-original download makes.
    """

    async def fetch(uuid: str) -> Optional[AsyncIterator[bytes]]:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        client = httpx.AsyncClient(timeout=httpx.Timeout(300, connect=15))

        try:
            response = await client.send(
                client.build_request(
                    "POST",
                    f"{settings.API_URL}/api/v1/transcriber/{uuid}/original",
                    json={"encryption_password": password},
                    headers=headers,
                ),
                stream=True,
            )
        except httpx.HTTPError:
            await client.aclose()
            return None

        if response.status_code != 200:
            await response.aclose()
            await client.aclose()
            return None

        async def chunks():
            try:
                async for piece in response.aiter_bytes():
                    yield piece
            finally:
                await response.aclose()
                await client.aclose()

        return chunks()

    return fetch


@app.get(ZIP_PREFIX + "/{token}")
async def export_zip(token: str) -> Response:
    """
    The ZIP an export registered, streamed as it is written. Single-use,
    and only for the session that registered it.
    """

    _sweep()
    entry = _pending.get(token)

    if entry is None or entry["owner"] != _signed_in_owner():
        return Response("Not found", status_code=404)

    password = None
    if entry["originals"]:
        password = _encryption_password()
        if not password:
            return Response("Not signed in", status_code=401)

    _pending.pop(token, None)

    fetch = _backend_fetch(password or "", app.storage.user.get("token"))

    return StreamingResponse(
        zip_stream(entry["files"], entry["originals"], fetch),
        media_type="application/zip",
        headers={
            "content-disposition": (
                f"attachment; filename*=UTF-8''{quote(entry['name'])}"
            ),
            "cache-control": "no-store",
        },
    )
