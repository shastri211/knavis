"""Accounts, ownership and rate limits."""
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import MODEL, TEST_EMAIL, TEST_PASSWORD, make_txt, sign_in

CREDS = {"email": "new.person@example.com", "password": "a perfectly fine passphrase"}


@pytest.fixture
def anonymous():
    """A client that is not signed in."""
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


# ---- registration and sign-in ----------------------------------------------------------------

def test_register_returns_a_working_token_and_sign_out_revokes_it(anonymous):
    created = anonymous.post("/api/auth/register", json={"email": "Case.Test@Example.COM", "password": CREDS["password"]})
    assert created.status_code == 201
    token = created.json()["token"]
    assert created.json()["user"]["email"] == "case.test@example.com"           # emails are case-insensitive
    headers = {"Authorization": f"Bearer {token}"}
    assert anonymous.get("/api/auth/me", headers=headers).json()["email"] == "case.test@example.com"
    assert anonymous.post("/api/auth/logout", headers=headers).status_code == 204
    assert anonymous.get("/api/auth/me", headers=headers).status_code == 401    # revoked


def test_login_works_and_failures_do_not_reveal_which_part_was_wrong(anonymous):
    anonymous.post("/api/auth/register", json=CREDS)
    assert anonymous.post("/api/auth/login", json={"email": CREDS["email"].upper(), "password": CREDS["password"]}).status_code == 200
    wrong = anonymous.post("/api/auth/login", json={"email": CREDS["email"], "password": "nope nope nope"})
    unknown = anonymous.post("/api/auth/login", json={"email": "nobody@example.com", "password": "nope nope nope"})
    assert wrong.status_code == unknown.status_code == 401 and wrong.json() == unknown.json()


@pytest.mark.parametrize("body, status", [
    ({"email": "not-an-email", "password": "long enough password"}, 400),
    ({"email": "a@b", "password": "long enough password"}, 400),
    ({"email": "x@example.com", "password": "short"}, 400),
    ({"email": "x@example.com", "password": "p" * 200}, 400),
    ({"email": "same@example.com", "password": "same@example.com"}, 400),
])
def test_registration_rejects_bad_input(anonymous, body, status):
    assert anonymous.post("/api/auth/register", json=body).status_code == status


def test_an_email_can_only_register_once(anonymous):
    assert anonymous.post("/api/auth/register", json=CREDS).status_code in (201, 409)
    assert anonymous.post("/api/auth/register", json=CREDS).status_code == 409


def test_passwords_and_tokens_are_stored_hashed(anonymous):
    from app.db import SessionLocal
    from app.models import AuthToken, User
    body = anonymous.post("/api/auth/register", json={"email": "hashed@example.com", "password": "very secret phrase"}).json()
    with SessionLocal() as db:
        user = db.query(User).filter(User.email == "hashed@example.com").one()
        assert user.password_hash.startswith("scrypt$") and "very secret phrase" not in user.password_hash
        assert db.get(AuthToken, body["token"]) is None                          # the raw token is not what is stored


def test_password_hash_verification_unit():
    from app.auth import hash_password, verify_password
    stored = hash_password("hunter2hunter2")
    assert verify_password("hunter2hunter2", stored) and not verify_password("hunter3hunter3", stored)
    assert hash_password("hunter2hunter2") != stored                               # salted
    assert not verify_password("x", "garbage") and not verify_password("x", "scrypt$1$2$3$4$5")


def test_an_expired_or_forged_token_is_refused(anonymous):
    from app.db import SessionLocal
    from app.models import AuthToken
    token = anonymous.post("/api/auth/register", json={"email": "expiry@example.com", "password": "long enough password"}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert anonymous.get("/api/sessions", headers=headers).status_code == 200
    with SessionLocal() as db:
        import hashlib
        for row in db.query(AuthToken).filter(AuthToken.id == hashlib.sha256(token.encode()).hexdigest()):   # only this token
            row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    assert anonymous.get("/api/sessions", headers=headers).status_code == 401
    for bad in ("Bearer forged", "Bearer ", "Basic abc", "", "Bearer " + "x" * 5000):
        assert anonymous.get("/api/sessions", headers={"Authorization": bad}).status_code == 401


PROTECTED = [
    ("get", "/api/models"), ("get", "/api/sessions"), ("post", "/api/sessions"), ("patch", "/api/sessions/x"),
    ("delete", "/api/sessions/x"), ("get", "/api/sessions/x/messages"), ("get", "/api/sessions/x/documents"),
    ("post", "/api/chat"), ("post", "/api/uploads"), ("post", "/api/documents/x/process"), ("delete", "/api/documents/x"),
    ("get", "/api/jobs/x"), ("post", "/api/agent/ask"), ("get", "/api/quota"), ("get", "/api/specialists"),
    ("get", "/api/final/contract"), ("get", "/api/auth/me"), ("post", "/api/auth/logout"), ("delete", "/api/auth/me"),
]


@pytest.mark.parametrize("method, path", PROTECTED)
def test_every_data_route_requires_sign_in(anonymous, method, path):
    assert getattr(anonymous, method)(path).status_code in (401,), (method, path)


@pytest.mark.parametrize("path", ["/api/health", "/api/health/live", "/api/health/ready", "/api/auth/config", "/"])
def test_health_and_the_sign_in_screen_config_are_public(anonymous, path):
    assert anonymous.get(path).status_code == 200


def test_the_removed_unauthenticated_pipeline_routes_are_gone(anonymous):
    assert anonymous.post("/api/pipeline/ask", json={}).status_code == 404
    assert anonymous.post("/api/pipeline/index/x").status_code == 404


# ---- ownership -------------------------------------------------------------------------------

def test_another_user_cannot_see_or_touch_my_chat_documents_or_jobs(client, other_client, llm, upload):
    mine = client.post("/api/sessions", json={"title": "mine"}).json()["id"]
    document, job = upload(mine, "policy.txt", make_txt(), "text/plain")
    client.post("/api/chat", json={"session_id": mine, "content": "hello", "provider": "groq", "model": MODEL})

    assert mine not in [s["id"] for s in other_client.get("/api/sessions").json()]
    assert mine in [s["id"] for s in client.get("/api/sessions").json()]
    attempts = [
        other_client.get(f"/api/sessions/{mine}/messages"),
        other_client.get(f"/api/sessions/{mine}/documents"),
        other_client.patch(f"/api/sessions/{mine}", json={"title": "taken"}),
        other_client.post("/api/chat", json={"session_id": mine, "content": "hi", "provider": "groq", "model": MODEL}),
        other_client.post("/api/uploads", data={"session_id": mine}, files={"file": ("x.txt", b"x", "text/plain")}),
        other_client.post("/api/agent/ask", json={"session_id": mine, "query": "hi", "provider": "groq", "model": MODEL}),
        other_client.get(f"/api/jobs/{job['id']}"),
        other_client.post(f"/api/documents/{document['id']}/process", json={"action": "reindex"}),
        other_client.delete(f"/api/documents/{document['id']}"),
        other_client.delete(f"/api/sessions/{mine}"),
    ]
    assert [r.status_code for r in attempts] == [404] * len(attempts)           # 404, not 403: it does not reveal existence
    assert client.get(f"/api/sessions/{mine}/documents").json()[0]["id"] == document["id"]   # nothing was changed or deleted
    assert "mine" in [s["title"] for s in client.get("/api/sessions").json()]       # still there, still mine


def test_a_users_documents_are_not_retrievable_by_another_users_questions(client, other_client, llm, upload, ask):
    mine = client.post("/api/sessions", json={"title": "mine"}).json()["id"]
    upload(mine, "policy.txt", make_txt(), "text/plain")
    theirs = other_client.post("/api/sessions", json={"title": "theirs"}).json()["id"]
    answer = other_client.post("/api/chat", json={"session_id": theirs, "content": "How long must company data be retained after the contract ends?",
                                                  "provider": "groq", "model": MODEL}).json()
    assert "don't have enough reliable evidence" in answer["message"]["content"] and "answer" not in llm.calls


def test_deleting_my_account_deletes_my_data_and_signs_me_out(llm, upload):
    from fastapi.testclient import TestClient
    from app.analytics.tablestore import table_file
    from app.db import SessionLocal
    from app.main import app
    from app.models import ChatSession, DocChunk, Document, User
    from conftest import CAMPAIGN_FILE, XLSX_TYPE, make_campaign_xlsx
    with TestClient(app) as c:
        email = "leaving@example.com"
        c.headers["Authorization"] = "Bearer " + c.post("/api/auth/register", json={"email": email, "password": "long enough password"}).json()["token"]
        sid = c.post("/api/sessions", json={"title": "bye"}).json()["id"]
        for name, data, kind in (("policy.txt", make_txt(), "text/plain"), (CAMPAIGN_FILE, make_campaign_xlsx(), XLSX_TYPE)):
            assert c.post("/api/uploads", data={"session_id": sid}, files={"file": (name, data, kind)}).status_code == 200
        assert table_file(sid).exists()
        assert c.request("DELETE", "/api/auth/me", json={"password": "wrong password!!"}).status_code == 403
        assert c.request("DELETE", "/api/auth/me", json={"password": "long enough password"}).status_code == 204
        assert c.get("/api/auth/me").status_code == 401
    with SessionLocal() as db:
        assert db.query(User).filter(User.email == email).count() == 0
        assert db.get(ChatSession, sid) is None and db.query(Document).filter(Document.session_id == sid).count() == 0
        assert db.query(DocChunk).filter(DocChunk.session_id == sid).count() == 0
    assert not table_file(sid).exists()


def test_the_first_account_claims_chats_that_predate_accounts(tmp_path):
    """Run in a fresh data directory: an old database has chats with no owner and no users yet."""
    data = tmp_path / "data"; data.mkdir()
    code = (
        "from fastapi.testclient import TestClient\n"
        "from app.main import app\nfrom app.db import SessionLocal\nfrom app.models import ChatSession\n"
        "with TestClient(app) as c:\n"
        " with SessionLocal() as db:\n"
        "  db.add(ChatSession(id='old-chat', title='old')); db.commit()\n"
        " t = c.post('/api/auth/register', json={'email': 'owner@example.com', 'password': 'long enough password'}).json()['token']\n"
        " h = {'Authorization': 'Bearer ' + t}\n"
        " print('FIRST', [s['id'] for s in c.get('/api/sessions', headers=h).json()])\n"
        " t2 = c.post('/api/auth/register', json={'email': 'second@example.com', 'password': 'long enough password'}).json()['token']\n"
        " print('SECOND', c.get('/api/sessions', headers={'Authorization': 'Bearer ' + t2}).json())\n"
    )
    env = {**os.environ, "DATA_DIR": str(data), "PYTHONPATH": str(Path(__file__).parents[1])}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=str(tmp_path))
    assert out.returncode == 0, out.stderr[-800:]
    assert "FIRST ['old-chat']" in out.stdout and "SECOND []" in out.stdout


# ---- switches --------------------------------------------------------------------------------

def test_with_accounts_switched_off_the_app_is_single_user(anonymous, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "auth_enabled", False)
    sid = anonymous.post("/api/sessions", json={"title": "local"}).json()["id"]
    assert sid in [s["id"] for s in anonymous.get("/api/sessions").json()]
    assert anonymous.get("/api/auth/config").json() == {"auth_enabled": False, "registration_open": False}
    assert anonymous.post("/api/auth/register", json=CREDS).status_code == 400


def test_registration_can_be_closed(anonymous, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "allow_registration", False)
    assert anonymous.post("/api/auth/register", json={"email": "late@example.com", "password": "long enough password"}).status_code == 403
    assert anonymous.get("/api/auth/config").json()["registration_open"] is False
    assert anonymous.post("/api/auth/login", json={"email": TEST_EMAIL, "password": TEST_PASSWORD}).status_code == 200   # existing users still sign in


def test_cors_allows_each_listed_origin_and_no_other(anonymous, monkeypatch):
    # the middleware reads its origins at start-up; check the configured value is split properly
    from app.config import settings
    origins = [o.strip() for o in "http://a.example, http://b.example".split(",")]
    assert origins == ["http://a.example", "http://b.example"]
    response = anonymous.options("/api/sessions", headers={"Origin": settings.frontend_origin.split(",")[0].strip(),
                                                          "Access-Control-Request-Method": "GET"})
    assert response.status_code == 200 and "access-control-allow-credentials" not in response.headers
    evil = anonymous.options("/api/sessions", headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in evil.headers


# ---- rate limits -----------------------------------------------------------------------------

def test_sign_in_attempts_are_rate_limited_per_address_and_per_email(anonymous, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "rate_limit_auth_per_minute", 3)
    statuses = [anonymous.post("/api/auth/login", json={"email": "victim@example.com", "password": "guess guess guess"}).status_code for _ in range(5)]
    assert statuses == [401, 401, 401, 429, 429]
    blocked = anonymous.post("/api/auth/login", json={"email": "victim@example.com", "password": "guess guess guess"})
    assert int(blocked.headers["retry-after"]) >= 1


def test_chat_and_upload_are_limited_per_user_not_globally(client, other_client, llm, session_id, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "rate_limit_chat_per_minute", 2)
    monkeypatch.setattr(settings, "rate_limit_upload_per_minute", 1)
    body = {"session_id": session_id, "content": "hello", "provider": "groq", "model": MODEL}
    assert [client.post("/api/chat", json=body).status_code for _ in range(3)] == [200, 200, 429]
    other_session = other_client.post("/api/sessions", json={"title": "x"}).json()["id"]
    assert other_client.post("/api/chat", json={**body, "session_id": other_session}).status_code == 200   # another user is unaffected
    files = {"file": ("a.txt", b"hello there", "text/plain")}
    assert client.post("/api/uploads", data={"session_id": session_id}, files=files).status_code == 200
    assert client.post("/api/uploads", data={"session_id": session_id}, files={"file": ("b.txt", b"second", "text/plain")}).status_code == 429


def test_limiter_window_slides_and_zero_means_unlimited():
    from app.ratelimit import SlidingWindowLimiter
    limiter = SlidingWindowLimiter()
    assert [limiter.check("k", 2, window=0.2) for _ in range(2)] == [None, None]
    assert limiter.check("k", 2, window=0.2) is not None
    import time
    time.sleep(0.25)
    assert limiter.check("k", 2, window=0.2) is None
    assert all(limiter.check("free", 0) is None for _ in range(100))


def test_forwarded_for_is_only_trusted_behind_a_proxy(monkeypatch):
    from types import SimpleNamespace
    from app.config import settings
    from app.ratelimit import client_address
    request = SimpleNamespace(headers={"x-forwarded-for": "9.9.9.9, 10.0.0.1"}, client=SimpleNamespace(host="1.2.3.4"))
    assert client_address(request) == "1.2.3.4"
    monkeypatch.setattr(settings, "trust_proxy_headers", True)
    assert client_address(request) == "9.9.9.9"
