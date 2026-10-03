"""Password reset by e-mail: only offered when SMTP is configured, single-use links, no account enumeration."""
import re
import socketserver
import threading
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
def anonymous():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def fresh_limiter():
    from app.ratelimit import limiter
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture
def mailbox(monkeypatch):
    """SMTP is 'configured' and every message that would be sent is collected."""
    from app import mailer
    from app.config import settings
    sent = []
    monkeypatch.setattr(settings, "smtp_host", "smtp.test")
    monkeypatch.setattr(settings, "smtp_from", "KNAVIS <no-reply@knavis.test>")
    monkeypatch.setattr(settings, "public_url", "https://knavis.example")
    monkeypatch.setattr(mailer, "send_mail", lambda to, subject, body: sent.append((to, subject, body)))
    return sent


def register(c, email, password="original long passphrase"):
    r = c.post("/api/auth/register", json={"email": email, "password": password})
    assert r.status_code == 201, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def link_token(body):
    return re.search(r"#reset=([\w-]+)", body).group(1)


def test_reset_is_not_offered_without_smtp(anonymous):
    assert anonymous.get("/api/auth/config").json()["password_reset"] is False
    r = anonymous.post("/api/auth/forgot", json={"email": "x@example.com"})
    assert r.status_code == 501 and "administrator" in r.json()["detail"]


def test_the_whole_flow_a_mailed_single_use_link_sets_a_new_password(anonymous, mailbox):
    old_session = register(anonymous, "forgetful@example.com")
    assert anonymous.get("/api/auth/config").json()["password_reset"] is True
    r = anonymous.post("/api/auth/forgot", json={"email": "Forgetful@Example.com"})
    assert r.status_code == 202
    [(to, subject, body)] = mailbox
    assert to == "forgetful@example.com" and "reset" in subject.lower() and "https://knavis.example/#reset=" in body
    token = link_token(body)

    assert anonymous.post("/api/auth/reset", json={"token": token, "new_password": "brand new long passphrase"}).status_code == 204
    assert anonymous.get("/api/auth/me", headers=old_session).status_code == 401                       # every sign-in ended
    assert anonymous.post("/api/auth/login", json={"email": "forgetful@example.com", "password": "original long passphrase"}).status_code == 401
    assert anonymous.post("/api/auth/login", json={"email": "forgetful@example.com", "password": "brand new long passphrase"}).status_code == 200
    again = anonymous.post("/api/auth/reset", json={"token": token, "new_password": "another long passphrase"})
    assert again.status_code == 400                                                                      # single use


def test_the_answer_is_identical_for_unknown_addresses_and_nothing_is_mailed(anonymous, mailbox):
    known = register(anonymous, "known@example.com")                                                     # noqa: F841
    a = anonymous.post("/api/auth/forgot", json={"email": "known@example.com"})
    b = anonymous.post("/api/auth/forgot", json={"email": "nobody.here@example.com"})
    assert a.status_code == b.status_code == 202 and a.json() == b.json()
    assert [m[0] for m in mailbox] == ["known@example.com"]


def test_only_the_newest_link_works(anonymous, mailbox):
    register(anonymous, "twice@example.com")
    anonymous.post("/api/auth/forgot", json={"email": "twice@example.com"})
    anonymous.post("/api/auth/forgot", json={"email": "twice@example.com"})
    first, second = (link_token(m[2]) for m in mailbox)
    assert anonymous.post("/api/auth/reset", json={"token": first, "new_password": "brand new long passphrase"}).status_code == 400
    assert anonymous.post("/api/auth/reset", json={"token": second, "new_password": "brand new long passphrase"}).status_code == 204


def test_expired_forged_and_weak_resets_are_refused(anonymous, mailbox):
    from app.db import SessionLocal
    from app.models import PasswordReset
    register(anonymous, "late@example.com")
    anonymous.post("/api/auth/forgot", json={"email": "late@example.com"})
    token = link_token(mailbox[0][2])
    assert anonymous.post("/api/auth/reset", json={"token": "forged", "new_password": "brand new long passphrase"}).status_code == 400
    assert anonymous.post("/api/auth/reset", json={"token": token, "new_password": "short"}).status_code == 400        # a weak password does not burn the link
    with SessionLocal() as db:
        for row in db.query(PasswordReset):
            row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
    assert anonymous.post("/api/auth/reset", json={"token": token, "new_password": "brand new long passphrase"}).status_code == 400
    assert anonymous.post("/api/auth/login", json={"email": "late@example.com", "password": "original long passphrase"}).status_code == 200


def test_only_a_hash_of_the_link_is_stored(anonymous, mailbox):
    from app.db import SessionLocal
    from app.models import PasswordReset
    register(anonymous, "hashed.reset@example.com")
    anonymous.post("/api/auth/forgot", json={"email": "hashed.reset@example.com"})
    token = link_token(mailbox[0][2])
    with SessionLocal() as db:
        assert db.get(PasswordReset, token) is None


def test_asking_for_links_is_rate_limited_per_address(anonymous, mailbox):
    register(anonymous, "spammed@example.com")
    codes = [anonymous.post("/api/auth/forgot", json={"email": "spammed@example.com"}).status_code for _ in range(5)]
    assert codes == [202, 202, 202, 429, 429] and len(mailbox) == 3


def test_a_mail_failure_never_breaks_or_reveals_anything(anonymous, mailbox, monkeypatch, caplog):
    from app import mailer
    register(anonymous, "broken.mail@example.com")

    def boom(*a):
        raise ConnectionRefusedError("smtp down")
    monkeypatch.setattr(mailer, "send_mail", boom)
    assert anonymous.post("/api/auth/forgot", json={"email": "broken.mail@example.com"}).status_code == 202
    assert "smtp down" in caplog.text and "broken.mail@example.com" not in caplog.text


def test_the_mailer_speaks_smtp(monkeypatch):
    """Against a tiny real SMTP server on localhost (no TLS, no login)."""
    from app import mailer
    from app.config import settings
    received = []

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            send = lambda line: self.wfile.write((line + "\r\n").encode())
            send("220 test")
            data_mode, message = False, []
            for raw in self.rfile:
                line = raw.decode().rstrip("\r\n")
                if data_mode:
                    if line == ".":
                        received.append("\n".join(message)); data_mode = False; send("250 queued")
                    else:
                        message.append(line)
                elif line.upper().startswith(("EHLO", "HELO")):
                    send("250 hello")
                elif line.upper().startswith(("MAIL", "RCPT", "RSET", "NOOP")):
                    send("250 ok")
                elif line.upper() == "DATA":
                    data_mode = True; send("354 go")
                elif line.upper() == "QUIT":
                    send("221 bye"); return

    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        monkeypatch.setattr(settings, "smtp_host", "127.0.0.1")
        monkeypatch.setattr(settings, "smtp_port", server.server_address[1])
        monkeypatch.setattr(settings, "smtp_starttls", False)
        monkeypatch.setattr(settings, "smtp_user", "")
        monkeypatch.setattr(settings, "smtp_from", "no-reply@knavis.test")
        mailer.send_mail("person@example.com", "Hello there", "Body line")
        server.shutdown()
    assert received and "Subject: Hello there" in received[0] and "To: person@example.com" in received[0] and "Body line" in received[0]
