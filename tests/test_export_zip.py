"""
One ZIP for every export of more than one file (utils/export_zip.py),
written as it is sent: built files first, then each original piped in.
"""

import io
import zipfile

import pytest

import utils.export_zip as export_zip


async def collect(stream) -> bytes:
    return b"".join([piece async for piece in stream])


def fetcher(originals: dict):
    """
    A stand-in for the backend: uuid -> bytes, sent in small pieces, or
    None for an original that cannot be had.
    """

    async def fetch(uuid):
        data = originals.get(uuid)
        if data is None:
            return None

        async def chunks():
            for start in range(0, len(data), 1000):
                yield data[start : start + 1000]

        return chunks()

    return fetch


@pytest.mark.asyncio
async def test_files_and_originals_arrive_in_one_readable_zip():
    audio = bytes(range(256)) * 40
    body = await collect(
        export_zip.zip_stream(
            [("Lecture.srt", b"1\n00:00:00,000 --> 00:00:01,000\nHej\n")],
            [("Lecture.webm", "job-1")],
            fetcher({"job-1": audio}),
        )
    )

    archive = zipfile.ZipFile(io.BytesIO(body))

    assert archive.namelist() == ["Lecture.srt", "Lecture.webm"]
    assert archive.read("Lecture.webm") == audio
    assert archive.testzip() is None


@pytest.mark.asyncio
async def test_a_missing_original_is_named_not_fatal():
    body = await collect(
        export_zip.zip_stream(
            [("a.srt", b"x")], [("gone.webm", "job-9")], fetcher({})
        )
    )

    archive = zipfile.ZipFile(io.BytesIO(body))

    assert archive.namelist() == ["a.srt", export_zip.MISSING_NOTE]
    assert "gone.webm" in archive.read(export_zip.MISSING_NOTE).decode()


def test_the_same_name_twice_is_kept_apart():
    assert export_zip.unique_names(["a.srt", "a.srt", "a.srt", "b"]) == [
        "a.srt",
        "a (2).srt",
        "a (3).srt",
        "b",
    ]


def test_a_link_is_for_its_owner_only(monkeypatch):
    monkeypatch.setattr(export_zip, "_signed_in_owner", lambda: "owner-a")
    link = export_zip.prepare_zip("x.zip", [("a.srt", b"x")])
    token = link.rsplit("/", 1)[1]

    assert export_zip._pending[token]["owner"] == "owner-a"

    # Nobody signed in: no link at all.
    monkeypatch.setattr(export_zip, "_signed_in_owner", lambda: None)
    assert export_zip.prepare_zip("x.zip", [("a.srt", b"x")]) is None


@pytest.mark.asyncio
async def test_a_link_works_once_and_not_for_someone_else(monkeypatch):
    monkeypatch.setattr(export_zip, "_signed_in_owner", lambda: "owner-a")
    link = export_zip.prepare_zip("x.zip", [("a.srt", b"x")])
    token = link.rsplit("/", 1)[1]

    monkeypatch.setattr(export_zip, "_signed_in_owner", lambda: "owner-b")
    assert (await export_zip.export_zip(token)).status_code == 404

    monkeypatch.setattr(export_zip, "_signed_in_owner", lambda: "owner-a")
    monkeypatch.setattr(
        export_zip.app, "storage", type("S", (), {"user": {}})(), raising=False
    )
    first = await export_zip.export_zip(token)
    assert first.status_code == 200

    assert (await export_zip.export_zip(token)).status_code == 404
