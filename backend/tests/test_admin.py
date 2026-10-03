"""Changing a password, and the operator CLI that stands in for a password-reset email."""
import pytest

from conftest import make_txt


@pytest.fixture
def anonymous():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c


def register(c, email, password="first long passphrase"):
    r = c.post("/api/auth/register", json={"email": email, "password": password})
    assert r.status_code == 201, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


# ---- changing your own password --------------------------------------------------------------

def test_changing_a_password_works_ends_other_sign_ins_and_keeps_this_one(anonymous):
    here = register(anonymous, "changer@example.com")
    elsewhere = {"Authorization": "Bearer " + anonymous.post("/api/auth/login", json={"email": "changer@example.com", "password": "first long passphrase"}).json()["token"]}
    r = anonymous.post("/api/auth/password", headers=here, json={"current_password": "first long passphrase", "new_password": "second long passphrase"})
    assert r.status_code == 204
    assert anonymous.get("/api/auth/me", headers=here).status_code == 200            # this sign-in survives
    assert anonymous.get("/api/auth/me", headers=elsewhere).status_code == 401       # the other one does not
    assert anonymous.post("/api/auth/login", json={"email": "changer@example.com", "password": "first long passphrase"}).status_code == 401
    assert anonymous.post("/api/auth/login", json={"email": "changer@example.com", "password": "second long passphrase"}).status_code == 200


@pytest.mark.parametrize("current, new, status", [
    ("wrong password!!", "second long passphrase", 403),
    ("first long passphrase", "short", 400),
    ("first long passphrase", "{email}", 400),
])
def test_changing_a_password_needs_the_right_current_one_and_a_decent_new_one(anonymous, current, new, status):
    email = f"pwcheck-{status}-{len(new)}@example.com"
    headers = register(anonymous, email)
    r = anonymous.post("/api/auth/password", headers=headers, json={"current_password": current, "new_password": new.format(email=email)})
    assert r.status_code == status
    assert anonymous.post("/api/auth/login", json={"email": email, "password": "first long passphrase"}).status_code == 200


def test_changing_a_password_requires_sign_in(anonymous):
    assert anonymous.post("/api/auth/password", json={"current_password": "x", "new_password": "y"}).status_code == 401


# ---- the operator CLI ------------------------------------------------------------------------

def run(*argv):
    from app.admin import main
    return main(list(argv))


def test_reset_password_changes_it_and_signs_the_account_out_everywhere(anonymous, capsys):
    headers = register(anonymous, "forgot@example.com")
    assert run("reset-password", "Forgot@Example.com", "--password", "a brand new passphrase") == 0
    assert "1 sign-in(s) ended" in capsys.readouterr().out
    assert anonymous.get("/api/auth/me", headers=headers).status_code == 401
    assert anonymous.post("/api/auth/login", json={"email": "forgot@example.com", "password": "first long passphrase"}).status_code == 401
    assert anonymous.post("/api/auth/login", json={"email": "forgot@example.com", "password": "a brand new passphrase"}).status_code == 200


def test_reset_password_rejects_unknown_accounts_and_weak_passwords(anonymous, capsys):
    register(anonymous, "weak@example.com")
    assert run("reset-password", "nobody@example.com", "--password", "a perfectly fine passphrase") == 1
    assert "No account" in capsys.readouterr().err
    assert run("reset-password", "weak@example.com", "--password", "short") == 1
    assert anonymous.post("/api/auth/login", json={"email": "weak@example.com", "password": "first long passphrase"}).status_code == 200


def test_create_user_works_when_registration_is_closed(anonymous, monkeypatch, capsys):
    from app.config import settings
    monkeypatch.setattr(settings, "allow_registration", False)
    assert run("create-user", "operator.made@example.com", "--password", "an operator chosen passphrase") == 0
    assert anonymous.post("/api/auth/login", json={"email": "operator.made@example.com", "password": "an operator chosen passphrase"}).status_code == 200
    assert run("create-user", "operator.made@example.com", "--password", "an operator chosen passphrase") == 1      # already exists
    assert run("create-user", "not-an-email", "--password", "an operator chosen passphrase") == 1


def test_users_and_usage_report_chats_documents_and_storage(anonymous, capsys):
    headers = register(anonymous, "reporter@example.com")
    sid = anonymous.post("/api/sessions", json={"title": "r"}, headers=headers).json()["id"]
    anonymous.post("/api/uploads", headers=headers, data={"session_id": sid}, files={"file": ("a.txt", make_txt(), "text/plain")})
    from app.admin import list_users, usage
    from app.db import SessionLocal
    with SessionLocal() as db:
        mine = next(u for u in list_users(db) if u["email"] == "reporter@example.com")
        assert (mine["chats"], mine["documents"]) == (1, 1) and mine["storage_mb"] >= 0 and mine["sign_ins"] == 1
        info = usage(db)
        assert info["users"] >= 1 and info["documents"] >= 1 and info["biggest"]
    assert run("users") == 0 and "reporter@example.com" in capsys.readouterr().out
    assert run("usage") == 0


def test_revoke_tokens_for_one_account_or_everyone(anonymous, capsys):
    a = register(anonymous, "rev.a@example.com")
    b = register(anonymous, "rev.b@example.com")
    assert run("revoke-tokens", "rev.a@example.com") == 0
    assert anonymous.get("/api/auth/me", headers=a).status_code == 401 and anonymous.get("/api/auth/me", headers=b).status_code == 200
    assert run("revoke-tokens", "missing@example.com") == 1
    # (revoking everyone would sign the shared test client out, so only the single-account form is exercised here)


def test_delete_user_needs_confirmation_and_removes_everything(anonymous, capsys):
    headers = register(anonymous, "gone@example.com")
    sid = anonymous.post("/api/sessions", json={"title": "g"}, headers=headers).json()["id"]
    anonymous.post("/api/uploads", headers=headers, data={"session_id": sid}, files={"file": ("b.txt", make_txt(), "text/plain")})
    assert run("delete-user", "gone@example.com") == 1 and "--yes" in capsys.readouterr().err        # nothing deleted
    assert anonymous.get("/api/auth/me", headers=headers).status_code == 200
    assert run("delete-user", "gone@example.com", "--yes") == 0
    from app.db import SessionLocal
    from app.models import ChatSession, Document, User
    with SessionLocal() as db:
        assert db.query(User).filter(User.email == "gone@example.com").count() == 0
        assert db.get(ChatSession, sid) is None and db.query(Document).filter(Document.session_id == sid).count() == 0
    assert anonymous.get("/api/auth/me", headers=headers).status_code == 401
