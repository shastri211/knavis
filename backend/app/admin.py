"""Operator commands. There is no mail service, so password resets and user management happen here.

    python -m app.admin users                       # accounts with their chats, documents and storage
    python -m app.admin create-user EMAIL           # for servers with ALLOW_REGISTRATION=false
    python -m app.admin reset-password EMAIL        # prompts for the new password; signs the account out everywhere
    python -m app.admin revoke-tokens [EMAIL]       # sign one account (or everyone) out
    python -m app.admin delete-user EMAIL --yes     # removes the account and all its data
    python -m app.admin usage                       # totals, and the biggest accounts by storage

Run it where the data lives (in Docker: ``docker compose exec backend python -m app.admin users``).
"""
import argparse
import getpass
import sys

from fastapi import HTTPException
from sqlalchemy.orm import Session

from .auth import MAX_PASSWORD, MIN_PASSWORD, check_new_password, delete_user, hash_password, normalise_email, set_password
from .db import SessionLocal, init_db
from .models import AuthToken, ChatSession, DocChunk, Document, User


class AdminError(Exception):
    pass


def _user(db: Session, email: str) -> User:
    user = db.query(User).filter(User.email == (email or "").strip().lower()).first()
    if not user:
        raise AdminError(f"No account for {email}.")
    return user


def _checked(call, *args):
    try:
        return call(*args)
    except HTTPException as exc:
        raise AdminError(exc.detail) from None


def list_users(db: Session) -> list[dict]:
    rows = []
    for user in db.query(User).order_by(User.created_at).all():
        sessions = [s for (s,) in db.query(ChatSession.id).filter(ChatSession.user_id == user.id)]
        documents = db.query(Document.metadata_json).filter(Document.session_id.in_(sessions)).all() if sessions else []
        rows.append({
            "email": user.email, "created": user.created_at.date().isoformat() if user.created_at else "",
            "chats": len(sessions), "documents": len(documents),
            "storage_mb": round(sum((m or {}).get("size_bytes", 0) for (m,) in documents) / 1_048_576, 2),
            "sign_ins": db.query(AuthToken).filter(AuthToken.user_id == user.id).count(),
        })
    return rows


def create_user(db: Session, email: str, password: str) -> User:
    email = _checked(normalise_email, email)
    if db.query(User.id).filter(User.email == email).first():
        raise AdminError(f"{email} already has an account.")
    _checked(check_new_password, password, email)
    user = User(email=email, password_hash=hash_password(password))
    db.add(user)
    db.commit()
    return user


def reset_password(db: Session, email: str, password: str) -> int:
    """Set a new password and end every sign-in of the account. Returns how many sign-ins were ended."""
    user = _user(db, email)
    _checked(check_new_password, password, user.email)
    return set_password(db, user, password)


def revoke_tokens(db: Session, email: str | None = None) -> int:
    query = db.query(AuthToken)
    if email:
        query = query.filter(AuthToken.user_id == _user(db, email).id)
    count = query.delete()
    db.commit()
    return count


def remove_user(db: Session, email: str) -> None:
    delete_user(db, _user(db, email))


def usage(db: Session) -> dict:
    users = list_users(db)
    return {
        "users": len(users), "chats": db.query(ChatSession).count(), "documents": db.query(Document).count(),
        "chunks": db.query(DocChunk).count(), "storage_mb": round(sum(u["storage_mb"] for u in users), 2),
        "biggest": sorted(users, key=lambda u: -u["storage_mb"])[:5],
    }


def _password(args) -> str:
    if args.password:
        return args.password
    first, second = getpass.getpass("New password: "), getpass.getpass("Repeat it: ")
    if first != second:
        raise AdminError("The two passwords differ.")
    return first


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("users")
    commands.add_parser("usage")
    for name in ("create-user", "reset-password"):
        sub = commands.add_parser(name)
        sub.add_argument("email")
        sub.add_argument("--password", help=f"{MIN_PASSWORD}-{MAX_PASSWORD} characters; prompted for if omitted (preferred: it stays out of shell history)")
    sub = commands.add_parser("revoke-tokens")
    sub.add_argument("email", nargs="?")
    sub = commands.add_parser("delete-user")
    sub.add_argument("email")
    sub.add_argument("--yes", action="store_true", help="confirm that the account and all its data are deleted")
    args = parser.parse_args(argv)

    init_db()
    try:
        with SessionLocal() as db:
            if args.command == "users":
                rows = list_users(db)
                print(f"{'email':<36}{'created':<12}{'chats':>6}{'docs':>6}{'MB':>9}{'sign-ins':>10}")
                for u in rows:
                    print(f"{u['email']:<36}{u['created']:<12}{u['chats']:>6}{u['documents']:>6}{u['storage_mb']:>9}{u['sign_ins']:>10}")
                print(f"{len(rows)} account(s)")
            elif args.command == "usage":
                info = usage(db)
                print({k: v for k, v in info.items() if k != "biggest"})
                for u in info["biggest"]:
                    print(f"  {u['email']}: {u['storage_mb']} MB, {u['documents']} documents")
            elif args.command == "create-user":
                print(f"Created {create_user(db, args.email, _password(args)).email}.")
            elif args.command == "reset-password":
                revoked = reset_password(db, args.email, _password(args))
                print(f"Password changed for {args.email.strip().lower()}; {revoked} sign-in(s) ended.")
            elif args.command == "revoke-tokens":
                print(f"{revoke_tokens(db, args.email)} sign-in(s) ended.")
            elif args.command == "delete-user":
                if not args.yes:
                    raise AdminError("This deletes the account and all its chats and documents. Re-run with --yes.")
                remove_user(db, args.email)
                print(f"Deleted {args.email.strip().lower()} and everything it owned.")
    except AdminError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
