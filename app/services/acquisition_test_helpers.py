"""Akquise Test-Modus Helpers.

Steuert den Sandbox-Modus fuer sicheres Testen ohne echte Kunden.
Konfiguriert ueber system_settings: acquisition_test_mode, acquisition_test_email.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.settings import SystemSetting


async def is_test_mode(db: AsyncSession) -> bool:
    """Prueft ob Akquise-Test-Modus aktiv ist."""
    result = await db.execute(
        select(SystemSetting).where(SystemSetting.key == "acquisition_test_mode")
    )
    row = result.scalar_one_or_none()
    return row is not None and row.value == "true"


async def get_test_email(db: AsyncSession) -> str:
    """Gibt die Test-E-Mail-Adresse zurueck (alle Akquise-Mails gehen hierhin im Test-Modus)."""
    result = await db.execute(
        select(SystemSetting).where(SystemSetting.key == "acquisition_test_email")
    )
    row = result.scalar_one_or_none()
    return row.value if row else "hamdard@sincirus.com"


def override_email_if_test(
    to_email: str,
    subject: str,
    test_mode: bool,
    test_email: str,
) -> tuple[str, str]:
    """Ueberschreibt Empfaenger + Betreff wenn Test-Modus aktiv.

    Returns:
        (to_email, subject) — im Test-Modus umgeleitet + [TEST] Prefix
    """
    if test_mode:
        return test_email, f"[TEST] {subject}"
    return to_email, subject
