"""DomainProtectionService — Domain-Limits + Konsistenz fuer E-Mail-Versand.

Schuetzt Domains vor Uebernutzung und stellt sicher, dass eine Firma
immer von der gleichen Domain kontaktiert wird.

Domain-Limits (Default, konfigurierbar via system_settings):
- sincirus.com (M365): 50/Tag (alle 4 Postfaecher zusammen)
- sincirus-karriere.de (IONOS): 40/Tag (2 Postfaecher)
- jobs-sincirus.com (IONOS): 40/Tag (2 Postfaecher)
- Gesamt: ~130/Tag (inkl. Follow-Ups!)

Bounce-Schwellen:
- Warnung: 3%
- Pause: 5%
- Spam-Complaints: 0.3% Sofort-Stopp
"""

import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# ── Default Domain-Limits ──
DEFAULT_DOMAIN_LIMITS = {
    "sincirus.com": 50,
    "sincirus-karriere.de": 40,
    "jobs-sincirus.com": 40,
}

# ── Sending-Fenster ──
SENDING_WINDOWS = [
    (8, 0, 11, 30),   # 08:00-11:30
    (13, 30, 16, 0),   # 13:30-16:00
]

# ── Beste Versandtage (0=Mo, 6=So) ──
BEST_DAYS = {1, 2, 3}  # Di, Mi, Do
REDUCED_DAYS = {0, 4}   # Mo, Fr (-30%)


def get_domain_from_email(email: str) -> str:
    """Extrahiert Domain aus E-Mail-Adresse."""
    if "@" in email:
        return email.split("@", 1)[1].lower()
    return ""


async def get_domain_limits(db: AsyncSession) -> dict[str, int]:
    """Laedt Domain-Limits aus system_settings oder nutzt Defaults."""
    from app.models.settings import SystemSetting

    limits = dict(DEFAULT_DOMAIN_LIMITS)

    result = await db.execute(
        select(SystemSetting).where(
            SystemSetting.key.like("domain_limit_%")
        )
    )
    for setting in result.scalars().all():
        # Key: domain_limit_sincirus.com -> Domain: sincirus.com
        domain = setting.key.replace("domain_limit_", "")
        try:
            limits[domain] = int(setting.value)
        except ValueError:
            pass

    return limits


async def get_daily_send_count(db: AsyncSession, domain: str) -> int:
    """Zaehlt heute gesendete E-Mails fuer eine Domain (inkl. Follow-Ups + Akquise-Mails)."""
    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    result = await db.execute(
        text("""
            SELECT (
                SELECT COUNT(*) FROM client_presentations
                WHERE email_from LIKE :domain_pattern
                AND created_at >= :today_start
                AND status != 'cancelled'
            ) + (
                SELECT COUNT(*) FROM acquisition_emails
                WHERE from_email LIKE :domain_pattern
                AND sent_at >= :today_start
                AND status = 'sent'
            )
        """),
        {"domain_pattern": f"%@{domain}", "today_start": today_start},
    )
    return result.scalar() or 0


async def check_domain_capacity(
    db: AsyncSession,
    email_from: str,
    count: int = 1,
) -> dict:
    """Prueft ob eine Domain noch Kapazitaet hat.

    Returns:
        {
            "allowed": bool,
            "domain": str,
            "sent_today": int,
            "limit": int,
            "remaining": int,
            "is_reduced_day": bool,
        }
    """
    domain = get_domain_from_email(email_from)
    if not domain:
        return {"allowed": False, "domain": "", "reason": "Ungueltige E-Mail"}

    limits = await get_domain_limits(db)
    limit = limits.get(domain, 30)  # Unbekannte Domains: konservativ 30/Tag

    # Mo/Fr: Limit um 30% reduzieren
    today = datetime.now(timezone.utc)
    is_reduced = today.weekday() in REDUCED_DAYS
    effective_limit = int(limit * 0.7) if is_reduced else limit

    sent_today = await get_daily_send_count(db, domain)
    remaining = max(0, effective_limit - sent_today)

    return {
        "allowed": remaining >= count,
        "domain": domain,
        "sent_today": sent_today,
        "limit": effective_limit,
        "remaining": remaining,
        "is_reduced_day": is_reduced,
    }


async def get_domain_for_company(
    db: AsyncSession,
    company_name: str,
) -> str | None:
    """Findet die Domain die zuvor fuer diese Firma verwendet wurde.

    Domain-Konsistenz: Gleiche Firma IMMER von gleicher Domain anschreiben.
    Returns None wenn keine vorherige Kontaktierung.
    """
    result = await db.execute(
        text("""
            SELECT cp.email_from
            FROM client_presentations cp
            JOIN companies c ON cp.company_id = c.id
            WHERE LOWER(c.name) = LOWER(:company_name)
            AND cp.status != 'cancelled'
            ORDER BY cp.created_at DESC
            LIMIT 1
        """),
        {"company_name": company_name},
    )
    row = result.first()
    if row and row[0]:
        return get_domain_from_email(row[0])
    return None


def select_best_mailbox(
    mailboxes: list[dict],
    preferred_domain: str | None = None,
    exclude_domains: list[str] | None = None,
    mailbox_counts: dict[str, int] | None = None,
) -> dict | None:
    """Waehlt das beste Postfach mit Round-Robin-Rotation.

    Strategie:
    1. Domain-Konsistenz: Wenn preferred_domain gesetzt, NUR Postfaecher dieser Domain
    2. Innerhalb der Domain: Das Postfach mit den WENIGSTEN bisherigen Sends (Round-Robin)
    3. Fallback: Wenn keine preferred_domain, alle verfuegbaren Domains, wenigste Sends zuerst

    Args:
        mailboxes: Liste der verfuegbaren Postfaecher
        preferred_domain: Bevorzugte Domain (Domain-Konsistenz)
        exclude_domains: Domains die ausgeschlossen werden sollen (Limit erreicht)
        mailbox_counts: Dict {email: anzahl_sends} fuer Round-Robin (optional)

    Returns:
        Mailbox-Dict oder None wenn keines verfuegbar.
    """
    exclude = set(exclude_domains or [])
    counts = mailbox_counts or {}

    def _pick_least_used(candidates: list[dict]) -> dict | None:
        """Waehlt aus den Kandidaten das Postfach mit den wenigsten Sends."""
        if not candidates:
            return None
        if not counts:
            return candidates[0]
        return min(candidates, key=lambda mb: counts.get(mb["email"], 0))

    # Erst: Bevorzugte Domain (Domain-Konsistenz) — Round-Robin innerhalb
    if preferred_domain:
        domain_mailboxes = [
            mb for mb in mailboxes
            if get_domain_from_email(mb["email"]) == preferred_domain
            and get_domain_from_email(mb["email"]) not in exclude
        ]
        result = _pick_least_used(domain_mailboxes)
        if result:
            return result

    # Fallback: Alle verfuegbaren Domains — Round-Robin ueber alle
    all_available = [
        mb for mb in mailboxes
        if get_domain_from_email(mb["email"]) not in exclude
    ]
    return _pick_least_used(all_available)


def is_in_sending_window() -> bool:
    """Prueft ob aktuell ein Sending-Fenster offen ist (CET/CEST)."""
    # Deutschland ist UTC+1 (CET) oder UTC+2 (CEST)
    # Vereinfacht: UTC+1 als Basis
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc + timedelta(hours=1)  # Approximation CET

    hour = now_local.hour
    minute = now_local.minute
    current_minutes = hour * 60 + minute

    for start_h, start_m, end_h, end_m in SENDING_WINDOWS:
        window_start = start_h * 60 + start_m
        window_end = end_h * 60 + end_m
        if window_start <= current_minutes <= window_end:
            return True

    return False


async def get_all_domain_stats(db: AsyncSession) -> list[dict]:
    """Liefert Statistiken fuer alle Domains (fuer Dashboard/Debug).

    Returns:
        Liste von {domain, sent_today, limit, remaining, percentage}
    """
    limits = await get_domain_limits(db)
    today = datetime.now(timezone.utc)
    is_reduced = today.weekday() in REDUCED_DAYS

    stats = []
    for domain, base_limit in limits.items():
        effective_limit = int(base_limit * 0.7) if is_reduced else base_limit
        sent = await get_daily_send_count(db, domain)
        remaining = max(0, effective_limit - sent)
        stats.append({
            "domain": domain,
            "sent_today": sent,
            "limit": effective_limit,
            "base_limit": base_limit,
            "remaining": remaining,
            "percentage": round(sent / effective_limit * 100, 1) if effective_limit > 0 else 0,
            "is_reduced_day": is_reduced,
        })

    return stats
