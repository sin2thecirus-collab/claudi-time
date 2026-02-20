"""Zentraler Personen-Such-Service fuer den Telegram Bot.

Wird von allen Bot-Handlern verwendet (Email, Call, Tasks etc.).
Gibt bei mehreren Treffern reichen Kontext zurueck fuer Disambiguierung:
Stadt, Firma, letzter Kontakt, wann hinzugefuegt, Position.
"""

import logging
from datetime import datetime

from sqlalchemy import or_, select

logger = logging.getLogger(__name__)


async def search_persons(name: str, limit: int = 5) -> list[dict]:
    """Sucht Kandidaten, Kontakte und Unternehmen per Name.

    Returns: Liste von Dicts mit reichem Kontext:
    {
        "name": str,
        "email": str | None,
        "type": "candidate" | "contact" | "company",
        "id": str (UUID),
        "first_name": str,
        "salutation": str,          # "Herr" / "Frau"
        "city": str | None,
        "company_name": str | None,
        "position": str | None,
        "last_contact": datetime | None,
        "created_at": datetime | None,
        "display_context": str,      # Einzeiler fuer Telegram-Buttons
    }
    """
    results = []
    try:
        from app.database import async_session_maker
        from app.models.candidate import Candidate
        from app.models.company import Company
        from app.models.company_contact import CompanyContact

        search = f"%{name}%"

        # ── 1. Kandidaten suchen ──
        async with async_session_maker() as db:
            result = await db.execute(
                select(Candidate)
                .where(
                    or_(
                        Candidate.last_name.ilike(search),
                        (Candidate.first_name + " " + Candidate.last_name).ilike(search),
                        Candidate.first_name.ilike(search),
                    )
                )
                .limit(limit)
            )
            candidates = result.scalars().all()

            for c in candidates:
                full_name = f"{c.first_name or ''} {c.last_name or ''}".strip()
                context_parts = []
                if c.city:
                    context_parts.append(c.city)
                if c.current_company:
                    context_parts.append(c.current_company)
                elif c.current_position:
                    context_parts.append(c.current_position)
                if c.last_contact:
                    context_parts.append(f"Kontakt: {_format_relative_date(c.last_contact)}")
                elif c.created_at:
                    context_parts.append(f"Seit: {_format_relative_date(c.created_at)}")

                results.append({
                    "name": full_name,
                    "email": c.email,
                    "type": "candidate",
                    "id": str(c.id),
                    "first_name": c.first_name or "",
                    "last_name": c.last_name or "",
                    "salutation": c.gender or "",
                    "city": c.city,
                    "company_name": c.current_company,
                    "position": c.current_position,
                    "last_contact": c.last_contact,
                    "created_at": c.created_at,
                    "display_context": " | ".join(context_parts) if context_parts else "Kandidat",
                })

        # ── 2. Kontakte suchen ──
        async with async_session_maker() as db:
            result = await db.execute(
                select(CompanyContact)
                .where(
                    or_(
                        CompanyContact.last_name.ilike(search),
                        (CompanyContact.first_name + " " + CompanyContact.last_name).ilike(search),
                        CompanyContact.first_name.ilike(search),
                    )
                )
                .limit(limit)
            )
            contacts = result.scalars().all()

            for ct in contacts:
                ct_name = f"{ct.first_name or ''} {ct.last_name or ''}".strip() or "Unbekannt"
                context_parts = []
                if ct.city:
                    context_parts.append(ct.city)
                if ct.position:
                    context_parts.append(ct.position)
                if ct.created_at:
                    context_parts.append(f"Seit: {_format_relative_date(ct.created_at)}")

                # Firmenname nachladen (Relationship)
                company_name = None
                try:
                    if ct.company:
                        company_name = ct.company.name
                        context_parts.insert(0, company_name)
                except Exception:
                    pass

                results.append({
                    "name": ct_name,
                    "email": ct.email,
                    "type": "contact",
                    "id": str(ct.id),
                    "first_name": ct.first_name or "",
                    "last_name": ct.last_name or "",
                    "salutation": ct.salutation or "",
                    "city": ct.city,
                    "company_name": company_name,
                    "position": ct.position,
                    "last_contact": None,
                    "created_at": ct.created_at,
                    "display_context": " | ".join(context_parts) if context_parts else "Kontakt",
                })

        # ── 3. Unternehmen suchen ──
        async with async_session_maker() as db:
            result = await db.execute(
                select(Company)
                .where(Company.name.ilike(search))
                .limit(limit)
            )
            companies = result.scalars().all()

            for co in companies:
                context_parts = []
                if co.city:
                    context_parts.append(co.city)
                if co.industry:
                    context_parts.append(co.industry)
                if co.employee_count:
                    context_parts.append(f"{co.employee_count} MA")

                results.append({
                    "name": co.name,
                    "email": None,
                    "type": "company",
                    "id": str(co.id),
                    "first_name": "",
                    "last_name": "",
                    "salutation": "",
                    "city": co.city,
                    "company_name": co.name,
                    "position": None,
                    "last_contact": None,
                    "created_at": co.created_at,
                    "display_context": " | ".join(context_parts) if context_parts else "Unternehmen",
                })

        return results

    except Exception as e:
        logger.error(f"Personensuche fehlgeschlagen: {e}")
        return results


def build_disambiguation_buttons(
    matches: list[dict],
    callback_prefix: str,
    max_buttons: int = 5,
) -> list[list[dict]]:
    """Baut Inline-Keyboard Buttons fuer Empfaenger-Auswahl.

    Args:
        matches: Liste von search_persons() Ergebnissen
        callback_prefix: z.B. "email_pick_", "call_pick_", "task_pick_"
        max_buttons: Max. Anzahl Buttons (Default: 5)

    Returns: Telegram inline_keyboard Array
    """
    type_labels = {"candidate": "Kandidat", "contact": "Kontakt", "company": "Unternehmen"}
    buttons = []
    for i, m in enumerate(matches[:max_buttons]):
        typ = type_labels.get(m["type"], m["type"])
        label = f"{m['name']} ({typ})"
        if m.get("display_context"):
            label += f" — {m['display_context']}"
        # Telegram Button-Text max 64 Zeichen
        if len(label) > 64:
            label = label[:61] + "..."
        buttons.append([{
            "text": label,
            "callback_data": f"{callback_prefix}{i}",
        }])
    buttons.append([{"text": "Abbrechen", "callback_data": f"{callback_prefix}cancel"}])
    return buttons


def build_disambiguation_text(matches: list[dict], name: str) -> str:
    """Baut den Nachrichtentext fuer die Disambiguierung."""
    type_labels = {"candidate": "Kandidat", "contact": "Kontakt", "company": "Unternehmen"}
    lines = [f"Mehrere Treffer fuer <b>{name}</b>:\n"]
    for i, m in enumerate(matches[:5], 1):
        typ = type_labels.get(m["type"], m["type"])
        line = f"{i}. <b>{m['name']}</b> ({typ})"
        if m.get("display_context"):
            line += f"\n   {m['display_context']}"
        lines.append(line)
    lines.append("\nWen meinst du?")
    return "\n".join(lines)


def _format_relative_date(dt: datetime) -> str:
    """Formatiert ein Datum relativ: 'vor 3 Tagen', 'vor 2 Wochen' etc."""
    if not dt:
        return ""
    now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
    diff = now - dt
    days = diff.days

    if days == 0:
        return "heute"
    elif days == 1:
        return "gestern"
    elif days < 7:
        return f"vor {days} Tagen"
    elif days < 30:
        weeks = days // 7
        return f"vor {weeks} Woche{'n' if weeks > 1 else ''}"
    elif days < 365:
        months = days // 30
        return f"vor {months} Monat{'en' if months > 1 else ''}"
    else:
        return dt.strftime("%d.%m.%Y")
