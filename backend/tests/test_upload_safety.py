"""Uploads are untrusted: type by content, size while streaming, archive/PDF/image bombs, names, per-user caps."""
import io
import zipfile

import pytest

from conftest import (CAMPAIGN_FILE, SAMPLES, XLSX_TYPE, make_campaign_xlsx, make_docx, make_pdf, make_txt)

EXE = b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 200


def post(client, session_id, name, data, kind="application/octet-stream"):
    return client.post("/api/uploads", data={"session_id": session_id}, files={"file": (name, data, kind)})


def png(size=(8, 8)):
    from PIL import Image
    out = io.BytesIO(); Image.new("RGB", size, "white").save(out, format="PNG")
    return out.getvalue()


def zip_bytes(entries):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return out.getvalue()


# ---- real type must match the extension ---------------------------------------------------

@pytest.mark.parametrize("kind", sorted(SAMPLES))
def test_every_genuine_sample_is_still_accepted(kind, client, session_id):
    name, build, content_type = SAMPLES[kind]
    assert post(client, session_id, name, build(), content_type).status_code == 200


BAD_UPLOADS = {
    "program renamed to pdf": ("report.pdf", lambda: EXE),
    "text posing as pdf": ("report.pdf", lambda: b"just text, not a pdf"),
    "pdf posing as docx": ("report.docx", make_pdf),
    "garbage after zip magic": ("report.xlsx", lambda: b"PK\x03\x04 but then garbage"),
    "program renamed to txt": ("notes.txt", lambda: EXE),
    "pdf renamed to txt": ("notes.txt", make_pdf),
    "docx renamed to txt": ("notes.txt", make_docx),
    "NUL bytes in csv": ("data.csv", lambda: b"a,b\n1,\x00\x00\x00\x002\n"),
    "not an image": ("photo.png", lambda: b"not an image at all"),
    "png under a jpg name": ("photo.jpg", png),
    "not audio": ("voice.mp3", lambda: b"definitely not audio"),
    "empty file": ("empty.txt", lambda: b""),
    "text posing as word 97": ("old.doc", lambda: b"plain text posing as a Word 97 file"),
}


@pytest.mark.parametrize("case", sorted(BAD_UPLOADS))
def test_content_that_contradicts_the_extension_is_refused(case, client, session_id):
    name, build = BAD_UPLOADS[case]
    response = post(client, session_id, name, build())
    assert response.status_code == 400, response.text[:200]


def test_utf16_text_with_a_byte_order_mark_is_not_mistaken_for_binary(client, session_id):
    data = "name,city\nÅsa,Köln\n".encode("utf-16")
    assert post(client, session_id, "people.csv", data, "text/csv").status_code == 200


def test_a_refused_upload_leaves_nothing_behind(client, session_id):
    from app.config import settings
    before = len(list(settings.upload_dir.glob("*")))
    assert post(client, session_id, "bad.pdf", EXE).status_code == 400
    assert len(list(settings.upload_dir.glob("*"))) == before
    assert client.get(f"/api/sessions/{session_id}/documents").json() == []


# ---- size ---------------------------------------------------------------------------------

def test_an_oversized_upload_is_stopped_while_streaming(client, session_id, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "max_upload_mb", 1)
    response = post(client, session_id, "big.txt", b"a" * (2 * 1024 * 1024), "text/plain")
    assert response.status_code == 413 and "1 MB" in response.json()["detail"]


def test_an_honest_oversized_upload_is_refused_from_its_declared_length(client, session_id, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "max_upload_mb", 1)
    response = client.post("/api/uploads", content=b"x" * 10, headers={"content-length": str(5 * 1024 * 1024), "content-type": "multipart/form-data; boundary=x"})
    assert response.status_code == 413


def test_read_limited_never_reads_far_past_the_limit():
    import asyncio
    from app.uploads import UploadTooLarge, read_limited

    class Source:
        def __init__(self): self.read_bytes = 0
        async def read(self, n):
            self.read_bytes += n
            return b"x" * n

    source = Source()
    with pytest.raises(UploadTooLarge):
        asyncio.run(read_limited(source, 3 * (1 << 20)))
    assert source.read_bytes <= 5 * (1 << 20)


# ---- bombs ---------------------------------------------------------------------------------

def test_a_zip_bomb_inside_an_office_file_is_refused_before_it_is_opened(client, session_id, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "max_unpacked_mb", 5)
    bomb = zip_bytes([("[Content_Types].xml", "<Types/>"), ("xl/worksheets/sheet1.xml", b"0" * (20 * 1024 * 1024))])
    assert len(bomb) < 100_000                                   # tiny on the wire, 20 MB when unpacked
    response = post(client, session_id, "data.xlsx", bomb, XLSX_TYPE)
    assert response.status_code == 400 and "unsafe size" in response.json()["detail"]


def test_an_office_file_with_too_many_parts_is_refused(client, session_id, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "max_archive_entries", 10)
    many = zip_bytes([(f"part{i}.xml", "<a/>") for i in range(50)])
    assert post(client, session_id, "parts.docx", many).status_code == 400


def test_a_pdf_with_too_many_pages_is_refused(client, session_id, monkeypatch):
    import fitz
    from app.config import settings
    pdf = fitz.open()
    for _ in range(3):
        pdf.new_page()
    data = pdf.tobytes(); pdf.close()
    monkeypatch.setattr(settings, "max_pdf_pages", 2)
    response = post(client, session_id, "long.pdf", data, "application/pdf")
    assert response.status_code == 400 and "3 pages" in response.json()["detail"]


def test_an_image_that_describes_a_huge_canvas_is_refused(client, session_id, monkeypatch):
    from PIL import Image
    from app.config import settings
    out = io.BytesIO(); Image.new("1", (12000, 12000)).save(out, format="PNG")   # a few KB on disk, 144 megapixels in memory
    assert len(out.getvalue()) < 200_000
    monkeypatch.setattr(settings, "max_image_megapixels", 50)
    assert post(client, session_id, "huge.png", out.getvalue(), "image/png").status_code == 400


# ---- names ---------------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("report.pdf", "report.pdf"),
    ("../../etc/passwd.txt", "passwd.txt"),
    ("..\\..\\windows\\system32\\evil.txt", "evil.txt"),
    ("tab\tand\nnewline.txt", "tabandnewline.txt"),
    ("gpj.‮txt", "gpj.txt"),                             # right-to-left override removed
    ("  .hidden.txt  ", "hidden.txt"),
    ("", "upload.bin"),
    ("....", "upload.bin"),
])
def test_filenames_are_cleaned(raw, expected):
    from app.uploads import clean_filename
    assert clean_filename(raw) == expected


def test_long_names_are_shortened_but_keep_their_extension():
    from app.uploads import clean_filename
    cleaned = clean_filename("a" * 400 + ".xlsx")
    assert len(cleaned) <= 120 and cleaned.endswith(".xlsx")


def test_the_stored_and_listed_name_is_the_cleaned_one(client, session_id):
    response = post(client, session_id, "../../sneaky‮.txt", make_txt(), "text/plain")
    assert response.status_code == 200 and response.json()["document"]["filename"] == "sneaky.txt"


# ---- caps ----------------------------------------------------------------------------------

def test_a_chat_can_only_hold_so_many_documents(client, session_id, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "max_documents_per_session", 2)
    assert [post(client, session_id, f"n{i}.txt", f"document number {i}".encode(), "text/plain").status_code for i in range(3)] == [200, 200, 400]


def test_a_user_cannot_exceed_their_storage_allowance_but_another_user_can_upload(client, other_client, monkeypatch):
    from app.config import settings
    first = client.post("/api/sessions", json={"title": "s"}).json()["id"]
    second = client.post("/api/sessions", json={"title": "s2"}).json()["id"]
    other = other_client.post("/api/sessions", json={"title": "o"}).json()["id"]
    used = sum((d["details"] or {}).get("size_bytes", 0) for sid in (first, second) for d in client.get(f"/api/sessions/{sid}/documents").json())
    assert used == 0
    from app.db import SessionLocal
    from app.models import ChatSession, Document
    with SessionLocal() as db:
        mine = db.query(ChatSession.id).filter(ChatSession.user_id == db.get(ChatSession, first).user_id)
        already = sum((m or {}).get("size_bytes", 0) for (m,) in db.query(Document.metadata_json).filter(Document.session_id.in_([i for (i,) in mine])))
    monkeypatch.setattr(settings, "max_user_storage_mb", 1)
    chunk = b"word " * 100_000                                        # 0.5 MB, different text each time
    results = [post(client, first, f"big{i}.txt", chunk + str(i).encode(), "text/plain").status_code for i in range(4)]
    assert 413 in results
    assert post(other_client, other, "fine.txt", b"small file from someone else", "text/plain").status_code == 200
    assert already >= 0
