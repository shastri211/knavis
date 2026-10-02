"""Sending e-mail over SMTP. Only used for password reset, and only when SMTP_HOST is set."""
import logging
import smtplib
import ssl
from email.message import EmailMessage

from .config import settings

logger = logging.getLogger("mragrag")


def configured() -> bool:
    return bool(settings.smtp_host and settings.smtp_from)


def send_mail(to: str, subject: str, body: str) -> None:
    """Send one plain-text message. Raises on failure; callers decide whether that matters."""
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = settings.smtp_from, to, subject
    message.set_content(body)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as server:
        if settings.smtp_starttls:
            server.starttls(context=ssl.create_default_context())
        if settings.smtp_user:
            server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(message)


def send_quietly(to: str, subject: str, body: str) -> None:
    """For background tasks: a mail failure is logged (without the address or body), never raised to the caller."""
    try:
        send_mail(to, subject, body)
    except Exception as exc:
        logger.warning("Could not send mail (%s: %s)", type(exc).__name__, str(exc)[:120])
