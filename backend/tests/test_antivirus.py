"""Virus scanning of uploads through clamd's INSTREAM protocol, against a stand-in clamd (the real daemon is exercised
by hand with docker-compose.scan.yml). The EICAR test string is assembled at run time so this file is not itself flagged."""
import socket
import socketserver
import struct
import threading

import pytest

from conftest import make_txt

EICAR = "X5O!P%@AP[4\\PZX54(P^)7CC)7}$" + "EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"


class FakeClamd:
    """Implements enough of clamd: zINSTREAM with length-prefixed chunks, answering OK or '<name> FOUND'."""

    def __init__(self, reply=None):
        self.streams, self.reply = [], reply
        outer = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                command = b""
                while not command.endswith(b"\0"):
                    command += self.request.recv(1)
                assert command == b"zINSTREAM\0"
                data = b""
                while True:
                    header = self._read(4)
                    (size,) = struct.unpack(">I", header)
                    if size == 0:
                        break
                    data += self._read(size)
                outer.streams.append(data)
                if outer.reply is not None:
                    answer = outer.reply
                else:
                    answer = "stream: Eicar-Test-Signature FOUND" if EICAR.encode() in data else "stream: OK"
                self.request.sendall(answer.encode() + b"\0")

            def _read(self, n):
                buf = b""
                while len(buf) < n:
                    chunk = self.request.recv(n - len(buf))
                    if not chunk:
                        raise ConnectionError("closed")
                    buf += chunk
                return buf

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def clamd(monkeypatch):
    from app.config import settings
    fake = FakeClamd()
    monkeypatch.setattr(settings, "clamav_host", "127.0.0.1")
    monkeypatch.setattr(settings, "clamav_port", fake.port)
    monkeypatch.setattr(settings, "clamav_timeout", 5.0)
    yield fake
    fake.close()


def dead_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def post(client, session_id, name, data, kind="text/plain"):
    return client.post("/api/uploads", data={"session_id": session_id}, files={"file": (name, data, kind)})


def test_scanning_is_off_unless_configured(client, session_id):
    from app import antivirus
    assert not antivirus.enabled()
    assert post(client, session_id, "eicar.txt", EICAR.encode()).status_code == 200      # nothing scans it, and nothing else objects


def test_a_clean_file_is_accepted_and_was_really_streamed_to_the_scanner(client, session_id, clamd):
    assert post(client, session_id, "ok.txt", make_txt()).status_code == 200
    assert clamd.streams == [make_txt()]


def test_an_infected_file_is_refused_and_leaves_nothing_behind(client, session_id, clamd):
    from app.config import settings
    before = len(list(settings.upload_dir.glob("*")))
    response = post(client, session_id, "invoice.txt", EICAR.encode())
    assert response.status_code == 400 and "Eicar-Test-Signature" in response.json()["detail"]
    assert len(list(settings.upload_dir.glob("*"))) == before
    assert client.get(f"/api/sessions/{session_id}/documents").json() == []


def test_a_large_upload_is_sent_in_many_chunks_and_arrives_intact(client, session_id, clamd):
    data = ("word " * 400_000).encode()                                                  # about 2 MB: many 64 KB frames
    assert post(client, session_id, "big.txt", data).status_code == 200
    assert clamd.streams == [data]


def test_when_the_scanner_is_down_uploads_continue_unless_it_is_required(client, session_id, monkeypatch, caplog):
    from app.config import settings
    monkeypatch.setattr(settings, "clamav_host", "127.0.0.1")
    monkeypatch.setattr(settings, "clamav_port", dead_port())
    monkeypatch.setattr(settings, "clamav_timeout", 1.0)
    assert post(client, session_id, "unscanned.txt", make_txt()).status_code == 200
    assert "accepted without a scan" in caplog.text


def test_when_the_scanner_is_down_and_required_uploads_are_refused_with_503(client, session_id, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "clamav_host", "127.0.0.1")
    monkeypatch.setattr(settings, "clamav_port", dead_port())
    monkeypatch.setattr(settings, "clamav_timeout", 1.0)
    monkeypatch.setattr(settings, "clamav_required", True)
    response = post(client, session_id, "blocked.txt", make_txt())
    assert response.status_code == 503 and "scanner" in response.json()["detail"]
    assert client.get(f"/api/sessions/{session_id}/documents").json() == []


def test_an_unintelligible_scanner_answer_counts_as_unavailable_not_as_clean(client, session_id, clamd, monkeypatch):
    from app.config import settings
    clamd.reply = "stream: INSTREAM size limit exceeded. ERROR"
    monkeypatch.setattr(settings, "clamav_required", True)
    assert post(client, session_id, "toobig.txt", make_txt()).status_code == 503
    monkeypatch.setattr(settings, "clamav_required", False)
    assert post(client, session_id, "toobig2.txt", make_txt() + b" more").status_code == 200


def test_the_scan_runs_after_the_cheap_checks(client, session_id, clamd):
    """A program renamed .pdf is refused on content before any bytes are sent to the scanner."""
    assert post(client, session_id, "fake.pdf", b"MZ\x90\x00" + b"\x00" * 100, "application/pdf").status_code == 400
    assert clamd.streams == []
