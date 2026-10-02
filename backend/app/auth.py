"""Accounts: registration, sign-in with revocable bearer tokens, and the ownership checks every route uses.

* Passwords are stored as salted scrypt hashes (standard library, no extra dependency).
* A sign-in is an opaque random token; only its SHA-256 is stored, so it can be revoked (sign out) and a leaked
  database does not leak working tokens.
* Every chat session belongs to a user and so does everything inside it. Someone else's session, document or job
  answers 404, exactly as if it did not exist.
* ``AUTH_ENABLED=false`` turns all of this off for a single-user local install: every route then sees every chat.
"""
import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from . import mailer
from .models import AuthToken, ChatSession, Document, Job, PasswordReset, User
from .ratelimit import client_address, enforce

logger = logging.getLogger("mragrag")

MIN_PASSWORD, MAX_PASSWORD = 8, 128
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}


# ---- passwords -----------------------------------------------------------------------------

def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return "scrypt${n}${r}${p}${salt}${hash}".format(
        **{k: _SCRYPT[k] for k in "nrp"}, salt=base64.b64encode(salt).decode(), hash=base64.b64encode(digest).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, expected = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(digest, base64.b64decode(expected))
    except (ValueError, TypeError):
        return False


_DUMMY_HASH = hash_password("not-a-real-password")   # compared against when the email is unknown, so timing does not reveal it


# ---- tokens --------------------------------------------------------------------------------

def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_token(db: Session, user: User) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    db.query(AuthToken).filter(AuthToken.expires_at < now).delete()   # tidy up expired sign-ins as a side effect
    db.add(AuthToken(id=_digest(token), user_id=user.id, expires_at=now + timedelta(days=settings.token_ttl_days)))
    db.commit()
    return token


# ---- who is asking -------------------------------------------------------------------------

@dataclass(frozen=True)
class Principal:
    user_id: str | None      # None: authentication is off and every chat is visible
    email: str | None = None


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None


def current_principal(request: Request, db: Session = Depends(get_db)) -> Principal:
    if not settings.auth_enabled:
        return Principal(None)
    token = _bearer(request)
    row = db.get(AuthToken, _digest(token)) if token else None
    expires = row.expires_at.replace(tzinfo=timezone.utc) if row and row.expires_at.tzinfo is None else (row.expires_at if row else None)
    user = db.get(User, row.user_id) if row and expires > datetime.now(timezone.utc) else None
    if not user:
        raise HTTPException(401, "Sign in to continue.", headers={"WWW-Authenticate": "Bearer"})
    return Principal(user.id, user.email)


def owned_session(db: Session, principal: Principal, session_id: str) -> ChatSession:
    session = db.get(ChatSession, session_id)
    if not session or (principal.user_id is not None and session.user_id != principal.user_id):
        raise HTTPException(404, "Session not found")
    return session


def owned_document(db: Session, principal: Principal, document_id: str) -> Document:
    document = db.get(Document, document_id)
    if not document:
        raise HTTPException(404, "Document not found")
    try:
        owned_session(db, principal, document.session_id)
    except HTTPException:
        raise HTTPException(404, "Document not found") from None
    return document


def owned_job(db: Session, principal: Principal, job_id: str) -> Job:
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    try:
        owned_session(db, principal, job.session_id)
    except HTTPException:
        raise HTTPException(404, "Job not found") from None
    return job


# ---- routes --------------------------------------------------------------------------------

router = APIRouter(prefix="/auth", tags=["auth"])


class Credentials(BaseModel):
    email: str
    password: str


class DeleteAccount(BaseModel):
    password: str


class Forgot(BaseModel):
    email: str


class Reset(BaseModel):
    token: str
    new_password: str


class ChangePassword(BaseModel):
    current_password: str
    new_password: str


def normalise_email(email: str) -> str:
    email = (email or "").strip().lower()
    if len(email) > 254 or not _EMAIL_RE.match(email):
        raise HTTPException(400, "Enter a valid email address.")
    return email


def check_new_password(password: str, email: str) -> None:
    if not MIN_PASSWORD <= len(password or "") <= MAX_PASSWORD:
        raise HTTPException(400, f"Use a password of {MIN_PASSWORD} to {MAX_PASSWORD} characters.")
    if password.lower() == email:
        raise HTTPException(400, "The password must not be your email address.")


def _session_out(db: Session, user: User) -> dict:
    return {"token": issue_token(db, user), "user": {"id": user.id, "email": user.email}}


@router.get("/config")
def auth_config():
    """What the sign-in screen needs to know before anyone is signed in."""
    return {"auth_enabled": settings.auth_enabled, "registration_open": settings.auth_enabled and settings.allow_registration,
            "password_reset": settings.auth_enabled and mailer.configured()}


@router.post("/register", status_code=201)
def register(body: Credentials, request: Request, db: Session = Depends(get_db)):
    if not settings.auth_enabled:
        raise HTTPException(400, "Accounts are turned off on this server.")
    if not settings.allow_registration:
        raise HTTPException(403, "Registration is closed on this server.")
    enforce("auth", client_address(request), settings.rate_limit_auth_per_minute)
    email = normalise_email(body.email)
    check_new_password(body.password, email)
    if db.query(User.id).filter(User.email == email).first():
        raise HTTPException(409, "An account with this email already exists.")
    first = db.query(User.id).first() is None
    user = User(email=email, password_hash=hash_password(body.password))
    db.add(user)
    db.flush()
    if first:   # chats created before accounts existed belong to whoever sets the server up
        claimed = db.query(ChatSession).filter(ChatSession.user_id.is_(None)).update({"user_id": user.id})
        if claimed:
            logger.info("The first account claimed %s existing chats", claimed)
    db.commit()
    return _session_out(db, user)


@router.post("/login")
def login(body: Credentials, request: Request, db: Session = Depends(get_db)):
    if not settings.auth_enabled:
        raise HTTPException(400, "Accounts are turned off on this server.")
    email = (body.email or "").strip().lower()
    enforce("auth", client_address(request), settings.rate_limit_auth_per_minute)
    enforce("auth-email", email, settings.rate_limit_auth_per_minute)
    user = db.query(User).filter(User.email == email).first()
    ok = verify_password(body.password or "", user.password_hash if user else _DUMMY_HASH)
    if not user or not ok:
        raise HTTPException(401, "Incorrect email or password.")
    return _session_out(db, user)


@router.post("/logout", status_code=204)
def logout(request: Request, db: Session = Depends(get_db), principal: Principal = Depends(current_principal)):
    token = _bearer(request)
    if token:
        db.query(AuthToken).filter(AuthToken.id == _digest(token)).delete()
        db.commit()


@router.get("/me")
def me(principal: Principal = Depends(current_principal)):
    return {"id": principal.user_id, "email": principal.email}


@router.delete("/me", status_code=204)
def delete_account(body: DeleteAccount, db: Session = Depends(get_db), principal: Principal = Depends(current_principal)):
    """Delete the account and everything it owns: chats, documents, vectors, tables and files."""
    if principal.user_id is None:
        raise HTTPException(400, "Accounts are turned off on this server.")
    user = db.get(User, principal.user_id)
    if not user or not verify_password(body.password or "", user.password_hash):
        raise HTTPException(403, "Incorrect password.")
    delete_user(db, user)


def delete_user(db: Session, user: User) -> None:
    """Delete an account and everything it owns: chats, documents, vectors, tables, files, sign-ins."""
    from .cleanup import delete_session
    from .integration.pipeline import get_pipeline
    for session in db.query(ChatSession).filter(ChatSession.user_id == user.id).all():
        delete_session(db, session, get_pipeline())
    db.query(AuthToken).filter(AuthToken.user_id == user.id).delete()
    db.delete(user)
    db.commit()


def set_password(db: Session, user: User, new_password: str, keep_token_id: str | None = None) -> int:
    """Store a new password and end every sign-in of the account (except ``keep_token_id``). Returns how many."""
    user.password_hash = hash_password(new_password)
    query = db.query(AuthToken).filter(AuthToken.user_id == user.id)
    if keep_token_id:
        query = query.filter(AuthToken.id != keep_token_id)
    revoked = query.delete()
    db.commit()
    return revoked


@router.post("/forgot", status_code=202)
def forgot_password(body: Forgot, request: Request, background: BackgroundTasks, db: Session = Depends(get_db)):
    """Mail a single-use reset link. The answer is the same whether or not the address has an account."""
    if not settings.auth_enabled or not mailer.configured():
        raise HTTPException(501, "Password reset by e-mail is not set up on this server. Ask its administrator to reset it.")
    email = (body.email or "").strip().lower()
    enforce("auth", client_address(request), settings.rate_limit_auth_per_minute)
    enforce("forgot-email", email, 3)   # a few a minute per address, so nobody can use this to spam an inbox
    user = db.query(User).filter(User.email == email).first()
    if user:
        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        db.query(PasswordReset).filter(PasswordReset.user_id == user.id, PasswordReset.used_at.is_(None)).delete()   # only the newest link works
        db.add(PasswordReset(id=_digest(token), user_id=user.id, expires_at=now + timedelta(minutes=settings.reset_token_minutes)))
        db.commit()
        base = (settings.public_url or str(request.base_url)).rstrip("/")
        background.add_task(mailer.send_quietly, user.email, "Reset your KNAVIS password",
                            f"Someone asked to reset the password of this KNAVIS account.\n\nOpen this link to choose a new one "
                            f"(it works once and expires in {settings.reset_token_minutes} minutes):\n\n{base}/#reset={token}\n\n"
                            "If it was not you, ignore this message: your password is unchanged.")
    return {"detail": "If that address has an account, a reset link is on its way."}


@router.post("/reset", status_code=204)
def reset_password_with_token(body: Reset, request: Request, db: Session = Depends(get_db)):
    """Set a new password using a link from the e-mail. Ends every sign-in of the account."""
    enforce("auth", client_address(request), settings.rate_limit_auth_per_minute)
    row = db.get(PasswordReset, _digest(body.token or ""))
    now = datetime.now(timezone.utc)
    expires = row.expires_at.replace(tzinfo=timezone.utc) if row and row.expires_at.tzinfo is None else (row.expires_at if row else None)
    user = db.get(User, row.user_id) if row and row.used_at is None and expires > now else None
    if not user:
        raise HTTPException(400, "This reset link is invalid or has expired. Ask for a new one.")
    check_new_password(body.new_password, user.email)
    row.used_at = now
    set_password(db, user, body.new_password)


@router.post("/password", status_code=204)
def change_password(body: ChangePassword, request: Request, db: Session = Depends(get_db),
                    principal: Principal = Depends(current_principal)):
    """Change your password. Every other sign-in of the account is ended; this one stays."""
    if principal.user_id is None:
        raise HTTPException(400, "Accounts are turned off on this server.")
    enforce("auth", client_address(request), settings.rate_limit_auth_per_minute)
    user = db.get(User, principal.user_id)
    if not user or not verify_password(body.current_password or "", user.password_hash):
        raise HTTPException(403, "Incorrect password.")
    check_new_password(body.new_password, user.email)
    token = _bearer(request)
    set_password(db, user, body.new_password, _digest(token) if token else None)
