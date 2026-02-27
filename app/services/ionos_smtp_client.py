"""IONOS SMTP Client — Versendet E-Mails ueber IONOS Mailboxen (STARTTLS).

Fuer die 4 IONOS-Postfaecher:
- hamdard@sincirus-karriere.de
- m.hamdard@sincirus-karriere.de
- m.hamdard@jobs-sincirus.com
- hamdard@jobs-sincirus.com

Konfiguration via ENV oder system_settings.
"""

import logging
from email.message import EmailMessage

import aiosmtplib

logger = logging.getLogger(__name__)

# IONOS SMTP-Server Konfiguration
IONOS_SMTP_HOST = "smtp.ionos.de"
IONOS_SMTP_PORT = 587  # STARTTLS

# IONOS-Domains — alles was NICHT sincirus.com ist
IONOS_DOMAINS = {"sincirus-karriere.de", "jobs-sincirus.com"}


def is_ionos_mailbox(email: str) -> bool:
    """Prueft ob eine E-Mail-Adresse zu einer IONOS-Domain gehoert."""
    if not email or "@" not in email:
        return False
    domain = email.split("@", 1)[1].lower()
    return domain in IONOS_DOMAINS


class IonosSmtpClient:
    """Sendet E-Mails via IONOS SMTP (STARTTLS, Port 587)."""

    @staticmethod
    async def send_email(
        to_email: str,
        subject: str,
        body_plain: str,
        from_email: str,
        password: str,
        in_reply_to: str | None = None,
    ) -> dict:
        """Sendet eine E-Mail via IONOS SMTP.

        Args:
            to_email: Empfaenger
            subject: Betreff
            body_plain: Klartext-Body (wird als text/plain gesendet)
            from_email: Absender (IONOS-Mailbox)
            password: SMTP-Passwort fuer diese Mailbox
            in_reply_to: Message-ID fuer Thread-Linking (optional)

        Returns:
            {"success": bool, "message_id": str | None, "error": str | None}
        """
        if not password:
            return {
                "success": False,
                "message_id": None,
                "error": f"Kein SMTP-Passwort fuer {from_email} konfiguriert",
            }

        msg = EmailMessage()
        msg["From"] = from_email
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.set_content(body_plain)

        # Thread-Linking Header
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to

        try:
            response = await aiosmtplib.send(
                msg,
                hostname=IONOS_SMTP_HOST,
                port=IONOS_SMTP_PORT,
                start_tls=True,
                username=from_email,
                password=password,
                timeout=30,
            )

            # aiosmtplib gibt Tuple (response_dict, message) zurueck
            logger.info(f"IONOS E-Mail gesendet: {from_email} → {to_email} | {subject}")
            return {
                "success": True,
                "message_id": msg.get("Message-ID", ""),
                "error": None,
            }

        except aiosmtplib.SMTPAuthenticationError as e:
            logger.error(f"IONOS Auth-Fehler fuer {from_email}: {e}")
            return {
                "success": False,
                "message_id": None,
                "error": f"SMTP-Authentifizierung fehlgeschlagen: {e}",
            }

        except aiosmtplib.SMTPException as e:
            logger.error(f"IONOS SMTP-Fehler: {e}")
            return {
                "success": False,
                "message_id": None,
                "error": f"SMTP-Fehler: {e}",
            }

        except Exception as e:
            logger.error(f"IONOS Unbekannter Fehler: {e}")
            return {
                "success": False,
                "message_id": None,
                "error": str(e),
            }
