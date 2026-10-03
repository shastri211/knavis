"""Confirming new accounts' e-mail addresses (REQUIRE_EMAIL_VERIFICATION with SMTP configured)."""
import re
from datetime import datetime, timedelta, timezone

import pytest

PASSWORD = "a perfectly fine passphrase"


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
    """SMTP is configured, verification is required, and every message that would be sent is collected."""
    from app import mailer
    from app.config import settings
    sent = []
    monkeypatch.setattr(settings, "smtp_host", "smtp.test")
    monkeypatch.setattr(settings, "smtp_from", "KNAVIS <no-reply@knavis.test>")
    monkeypatch.setattr(settings, "public_url", "https://knavis.example")
    monkeypatch.setattr(settings, "require_email_verification", True)
    monkeypatch.setattr(mailer, "send_mail", lambda to, subject, body: sent.append((to, subject, body)))
    return sent


def token_of(body):
    return re.search(r"#verify=([\w-]+)", body).group(1)


def register(c, email):
    return c.post("/api/auth/register", json={"email": email, "password": PASSWORD})


def test_verification_needs_smtp_and_the_setting(anonymous, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "require_email_verification", True)            # no SMTP: cannot work, so it is not enforced
    assert anonymous.get("/api/auth/config").json()["email_verification"] is False
    r = register(anonymous, "plain@example.com")
    assert r.status_code == 201 and "token" in r.json()                          # signs in at once, as before
    assert anonymous.post("/api/auth/resend", json={"email": "plain@example.com"}).status_code == 501


def test_registration_creates_an_account_that_cannot_sign_in_until_the_link_is_opened(anonymous, mailbox):
    assert anonymous.get("/api/auth/config").json()["email_verification"] is True
    r = register(anonymous, "newcomer@example.com")
    assert r.status_code == 201 and r.json() == {"verification_required": True, "email": "newcomer@example.com"}   # no token
    [(to, subject, body)] = mailbox
    assert to == "newcomer@example.com" and "confirm" in subject.lower() and "https://knavis.example/#verify=" in body

    blocked = anonymous.post("/api/auth/login", json={"email": "newcomer@example.com", "password": PASSWORD})
    assert blocked.status_code == 403 and "confirm" in blocked.json()["detail"].lower()

    opened = anonymous.post("/api/auth/verify", json={"token": token_of(body)})
    assert opened.status_code == 200
    headers = {"Authorization": "Bearer " + opened.json()["token"]}                # opening the link signs the person in
    assert anonymous.get("/api/auth/me", headers=headers).json()["email"] == "newcomer@example.com"
    assert anonymous.post("/api/auth/login", json={"email": "newcomer@example.com", "password": PASSWORD}).status_code == 200


def test_a_wrong_password_still_looks_the_same_for_unconfirmed_accounts(anonymous, mailbox):
    register(anonymous, "quiet@example.com")
    wrong = anonymous.post("/api/auth/login", json={"email": "quiet@example.com", "password": "not the password!!"})
    unknown = anonymous.post("/api/auth/login", json={"email": "nobody@example.com", "password": "not the password!!"})
    assert wrong.status_code == unknown.status_code == 401 and wrong.json() == unknown.json()   # confirmation status is not leaked


def test_the_link_works_once_expires_and_cannot_be_forged(anonymous, mailbox):
    from app.db import SessionLocal
    from app.models import EmailVerification
    register(anonymous, "once@example.com")
    token = token_of(mailbox[0][2])
    assert anonymous.post("/api/auth/verify", json={"token": "forged"}).status_code == 400
    assert anonymous.post("/api/auth/verify", json={"token": token}).status_code == 200
    assert anonymous.post("/api/auth/verify", json={"token": token}).status_code == 400          # single use
    register(anonymous, "late.verify@example.com")
    late = token_of(mailbox[-1][2])
    with SessionLocal() as db:
        for row in db.query(EmailVerification).filter(EmailVerification.used_at.is_(None)):
            row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
    assert anonymous.post("/api/auth/verify", json={"token": late}).status_code == 400
    assert anonymous.post("/api/auth/login", json={"email": "late.verify@example.com", "password": PASSWORD}).status_code == 403


def test_only_a_hash_of_the_link_is_stored(anonymous, mailbox):
    from app.db import SessionLocal
    from app.models import EmailVerification
    register(anonymous, "hash.verify@example.com")
    token = token_of(mailbox[0][2])
    with SessionLocal() as db:
        assert db.get(EmailVerification, token) is None


def test_asking_again_sends_a_fresh_link_and_only_the_newest_works(anonymous, mailbox):
    register(anonymous, "again@example.com")
    first = token_of(mailbox[0][2])
    r = anonymous.post("/api/auth/resend", json={"email": "again@example.com"})
    assert r.status_code == 202
    second = token_of(mailbox[1][2])
    assert anonymous.post("/api/auth/verify", json={"token": first}).status_code == 400
    assert anonymous.post("/api/auth/verify", json={"token": second}).status_code == 200


def test_resend_answers_the_same_for_unknown_and_already_confirmed_addresses_and_mails_nothing(anonymous, mailbox):
    register(anonymous, "done@example.com")
    anonymous.post("/api/auth/verify", json={"token": token_of(mailbox[0][2])})
    mailed = len(mailbox)
    answers = [anonymous.post("/api/auth/resend", json={"email": e}) for e in ("done@example.com", "ghost@example.com")]
    assert [a.status_code for a in answers] == [202, 202] and answers[0].json() == answers[1].json()
    assert len(mailbox) == mailed


def test_resend_is_rate_limited_per_address(anonymous, mailbox):
    register(anonymous, "spam.verify@example.com")
    codes = [anonymous.post("/api/auth/resend", json={"email": "spam.verify@example.com"}).status_code for _ in range(5)]
    assert codes == [202, 202, 202, 429, 429]


def test_a_password_reset_link_also_confirms_the_address(anonymous, mailbox):
    register(anonymous, "via.reset@example.com")
    assert anonymous.post("/api/auth/login", json={"email": "via.reset@example.com", "password": PASSWORD}).status_code == 403
    anonymous.post("/api/auth/forgot", json={"email": "via.reset@example.com"})
    reset_body = mailbox[-1][2]
    token = re.search(r"#reset=([\w-]+)", reset_body).group(1)
    assert anonymous.post("/api/auth/reset", json={"token": token, "new_password": "another good passphrase"}).status_code == 204
    assert anonymous.post("/api/auth/login", json={"email": "via.reset@example.com", "password": "another good passphrase"}).status_code == 200


def test_accounts_made_by_the_operator_are_confirmed_already(anonymous, mailbox):
    from app.admin import create_user
    from app.db import SessionLocal
    with SessionLocal() as db:
        create_user(db, "made.by.operator@example.com", PASSWORD)
    assert anonymous.post("/api/auth/login", json={"email": "made.by.operator@example.com", "password": PASSWORD}).status_code == 200


def test_existing_accounts_are_confirmed_by_the_migration_that_adds_the_column(tmp_path):
    """Turning the feature on later must not lock out people who signed up before it existed."""
    from alembic import command
    from sqlalchemy import create_engine, text
    from app.migrations import alembic_config, upgrade_database
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as connection:
        command.upgrade(alembic_config(connection), "0003")
        connection.execute(text("INSERT INTO users (id, email, password_hash, created_at) VALUES ('u', 'old@example.com', 'x', '2026-01-02 03:04:05')"))
    upgrade_database(engine)
    with engine.connect() as c:
        assert c.execute(text("SELECT email_verified_at FROM users WHERE id = 'u'")).scalar() is not None


def test_legacy_chats_go_to_the_first_confirmed_account_not_the_first_to_register(tmp_path):
    """In a fresh data directory (an old chat, no users): the squatter registers first but never confirms."""
    import os
    import subprocess
    import sys
    from pathlib import Path
    code = r"""
import re
from app import mailer
sent = []
mailer.send_mail = lambda to, subject, body: sent.append(body)
from fastapi.testclient import TestClient
from app.main import app
from app.db import SessionLocal
from app.models import ChatSession, User
with TestClient(app) as c:
    with SessionLocal() as db:
        db.add(ChatSession(id="old-chat", title="old")); db.commit()
    for email in ("squatter@example.com", "owner@example.com"):
        c.post("/api/auth/register", json={"email": email, "password": "a perfectly fine passphrase"})
    token = re.search(r"#verify=([\w-]+)", sent[1]).group(1)
    assert c.post("/api/auth/verify", json={"token": token}).status_code == 200
    with SessionLocal() as db:
        owner = db.query(User).filter(User.email == "owner@example.com").one()
        print("OWNER_GOT_THE_CHAT", db.get(ChatSession, "old-chat").user_id == owner.id)
"""
    env = {**os.environ, "DATA_DIR": str(tmp_path), "DATABASE_URL": "", "SMTP_HOST": "smtp.test", "SMTP_FROM": "no-reply@knavis.test",
           "REQUIRE_EMAIL_VERIFICATION": "true", "PUBLIC_URL": "https://knavis.example", "PYTHONPATH": str(Path(__file__).parents[1])}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=180)
    assert out.returncode == 0, out.stderr[-800:]
    assert "OWNER_GOT_THE_CHAT True" in out.stdout
