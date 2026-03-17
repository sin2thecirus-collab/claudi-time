"""PresentationReplyService — Reply Monitor fuer Vorstellungs-E-Mails.

Wird von n8n aufgerufen (alle 15 Min), klassifiziert Kunden-Antworten per GPT-4o-mini:
- POSITIVE: Interesse → Sequenz stoppen, Milad antwortet manuell
- NEGATIVE: Kein Interesse → Sequenz stoppen, freundliche Auto-Antwort senden
- DELETION_REQUEST: Loeschung verlangt → Alles loeschen, Domain blocklisten, Bestaetigung senden
- AUTO_REPLY: Abwesenheit/Lesebestaetigung → Komplett ignorieren, Sequenz laeuft weiter
"""

import json
import logging
import random
from datetime import datetime, timezone, timedelta
from typing import Optional
from uuid import UUID

from zoneinfo import ZoneInfo

from sqlalchemy import select, delete, func, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
#  REPLY CLASSIFICATION PROMPT (GPT-4o-mini)
# ═══════════════════════════════════════════════════════════════

REPLY_CLASSIFIER_SYSTEM = """Du bist ein Klassifikations-System fuer eingehende E-Mail-Antworten auf Personalvermittlungs-E-Mails.

Deine Aufgabe: Klassifiziere die Antwort in GENAU EINE Kategorie.

KATEGORIEN:

1. POSITIVE — Der Kunde zeigt echtes Interesse:
   - Moechte das vollstaendige Profil sehen
   - Moechte den Kandidaten kennenlernen / Gespraech fuehren
   - Fragt nach weiteren Informationen zum Kandidaten
   - Schlaegt Zusammenarbeit / Kooperation vor
   - Bittet um Rueckruf oder Terminvorschlag
   - Antwortet mit konkreten Fragen zum Kandidaten

2. NEGATIVE — Der Kunde lehnt ab:
   - Kein Interesse / kein Bedarf
   - Stelle bereits besetzt
   - Kandidat passt nicht
   - Arbeiten nicht mit Personalvermittlern
   - Haben eigene Recruiting-Abteilung
   - Hoefliche Absage jeglicher Art

3. DELETION_REQUEST — Der Kunde verlangt Datenloeschung:
   - Explizite Loeschungsanfrage ("loeschen Sie meine Daten", "aus Ihrem Verteiler entfernen")
   - Abmeldung / Unsubscribe-Wunsch
   - "Kontaktieren Sie mich nie wieder"
   - "Keine weiteren E-Mails"
   - DSGVO-Anfrage / Widerspruch gegen Datenverarbeitung
   - Drohung mit rechtlichen Schritten wegen unerwuenschter E-Mails

4. AUTO_REPLY — Automatische Systemnachrichten (KEIN Mensch hat geantwortet):
   - Abwesenheitsnotiz / Out-of-Office
   - Automatische Eingangsbestaetigung
   - Lesebestaetigung / Read Receipt
   - Delivery Notification / Zustellbestaetigung
   - Bounce-Nachricht (unzustellbar)
   - Mailbox voll / Quota exceeded
   - Typische Signalwoerter: "Abwesenheit", "out of office", "automatische Antwort",
     "automatic reply", "auto-reply", "Eingangsbestaetigung", "delivery notification",
     "Ihre Nachricht wurde empfangen", "nicht im Buero", "im Urlaub",
     "vacation", "holiday", "currently unavailable", "Rueckkehr am",
     "return on", "noreply", "no-reply", "do-not-reply", "mailer-daemon",
     "postmaster", "Unzustellbar", "Undeliverable"

WICHTIGE REGELN:
- AUTO_REPLY hat HOECHSTE Prioritaet: Wenn es automatisch generiert aussieht, ist es AUTO_REPLY
- Bei Unsicherheit zwischen POSITIVE und NEGATIVE: tendiere zu POSITIVE (lieber einmal zu viel Milad informieren)
- DELETION_REQUEST nur wenn EXPLIZIT Loeschung/Abmeldung verlangt wird — "kein Interesse" allein ist NEGATIVE, nicht DELETION_REQUEST
- Ignoriere E-Mail-Signaturen und Disclaimer bei der Klassifikation

BEISPIELE FUER GRENZFAELLE (alle POSITIVE!):

Beispiel 1: "Aktuell haben wir keinen Bedarf, aber schicken Sie uns gerne das Profil."
→ POSITIVE (Kunde will Profil sehen = Interesse!)

Beispiel 2: "Die Stelle ist bereits besetzt, aber wir suchen fuer eine andere Position jemanden. Koennen Sie mir mehr dazu sagen?"
→ POSITIVE (neues Geschaeft moeglich!)

Beispiel 3: "Danke fuer den Vorschlag. Gerade passt es zeitlich nicht, aber behalten Sie uns gerne im Hinterkopf."
→ POSITIVE (Kunde will zukuenftigen Kontakt = Beziehungspflege!)

Beispiel 4: "Wir haben eine eigene Recruiting-Abteilung, nehmen aber bei spezialisierten Profilen gerne Vorschlaege an."
→ POSITIVE (offen fuer Zusammenarbeit!)

Beispiel 5: "Rufen Sie mich bitte dazu an" oder "Koennen wir kurz telefonieren?"
→ POSITIVE (direktes Interesse an Kontakt!)

Beispiel 6: "Interessant. Was sind die Gehaltsvorstellungen?" oder "Wann waere der Kandidat verfuegbar?"
→ POSITIVE (konkrete Rueckfragen = hohes Interesse!)

Beispiel 7: "Nein danke, kein Interesse." oder "Bitte senden Sie uns keine weiteren Profile."
→ NEGATIVE (klare, eindeutige Absage ohne jede Oeffnung)

WICHTIG: Im Zweifel IMMER POSITIVE waehlen! Eine falsche NEGATIVE-Klassifikation fuehrt zu automatischer Absage und Geschaeftsverlust. Eine falsche POSITIVE-Klassifikation fuehrt nur dazu dass Milad eine Mail manuell beantwortet — das ist viel weniger schlimm.

Antworte als JSON:
{"category": "POSITIVE|NEGATIVE|DELETION_REQUEST|AUTO_REPLY", "confidence": 0.0-1.0, "reason": "Kurze Begruendung (1 Satz)", "absender_anrede": "Hallo Frau/Herr Nachname", "absender_nachname": "Nachname"}

ABSENDER-ANREDE: Extrahiere den Namen des Absenders aus der E-Mail (Signatur, From-Header, Anrede).
- Wenn Vorname + Nachname erkennbar: Bestimme das Geschlecht anhand des Vornamens und schreibe "Hallo Frau Nachname" oder "Hallo Herr Nachname"
- Wenn nur Nachname: "Hallo Frau/Herr Nachname" (beides angeben)
- Wenn gar kein Name erkennbar: "Hallo" (ohne Name)
- NIEMALS "Sehr geehrte Damen und Herren" — immer "Hallo"
"""

REPLY_CLASSIFIER_USER = """Klassifiziere diese E-Mail-Antwort:

Betreff: {subject}
Absender: {sender}

Nachricht:
{body}
"""


# ═══════════════════════════════════════════════════════════════
#  AUTO-REPLY TEMPLATES (NEGATIVE — menschlich klingend)
# ═══════════════════════════════════════════════════════════════

REPLY_GENERATOR_SYSTEM = """Du bist Milad Hamdard, Senior Personalberater bei Sincirus in Hamburg.
Schreibe eine kurze, menschliche Antwort auf eine Kunden-E-Mail. ICH-Form.

REGELN:
- Maximal 2-3 Saetze
- KEIN KI-Sprech, KEINE Floskeln wie "Ich verstehe Ihre Bedenken" oder "Ich schaetze Ihre Offenheit"
- Klingt wie ein normaler Mensch der schnell antwortet
- KEINE Signatur/Grussformel am Ende (wird automatisch angehaengt)
- Beginne die Antwort IMMER mit der Anrede die ich dir gebe
- Duze NICHT, sieze IMMER

TONALITAET: Locker-professionell, nicht steif, nicht unterwuerfig. Wie ein Kollege der kurz antwortet."""

REPLY_GENERATOR_NEGATIVE = """Schreibe eine kurze Antwort auf eine Absage. Der Kunde hat kein Interesse an meinem Kandidatenvorschlag.
Bedanke dich kurz, wuensche Erfolg, erwaehne dass man sich gerne wieder melden kann.
Beginne mit: {anrede}

Kunden-Nachricht (Kontext):
{reply_body}"""

REPLY_GENERATOR_DELETION = """Schreibe eine kurze Bestaetigung dass die Daten geloescht wurden.
Der Kunde hat um Loeschung seiner Daten / keine weiteren Kontaktaufnahmen gebeten.
Bestaetigen: Daten geloescht, keine weiteren E-Mails.
Beginne mit: {anrede}

Kunden-Nachricht (Kontext):
{reply_body}"""

PLAIN_TEXT_SIGNATURE = """
Milad Hamdard
Senior Personalberater | Rechnungswesen & Controlling
040 238 345 320   |   +49 176 8000 47 41
hamdard@sincirus.com
www.sincirus.com
Ballindamm 3, 20095 Hamburg
""".strip()


# ═══════════════════════════════════════════════════════════════
#  GESCHAEFTSZEITEN-HELPER
# ═══════════════════════════════════════════════════════════════

def _is_business_hours() -> bool:
    """Prueft ob aktuelle Zeit in Geschaeftszeiten liegt (8:00-18:00 Mo-Fr, Europe/Berlin)."""
    berlin = ZoneInfo("Europe/Berlin")
    now = datetime.now(berlin)
    # Mo=0, Fr=4
    if now.weekday() > 4:  # Sa/So
        return False
    return 8 <= now.hour < 18


def _next_business_morning() -> datetime:
    """Gibt den naechsten Werktag 8:00 Uhr zurueck (Europe/Berlin)."""
    berlin = ZoneInfo("Europe/Berlin")
    now = datetime.now(berlin)
    # Naechster Werktag
    next_day = now + timedelta(days=1)
    while next_day.weekday() > 4:  # Sa/So ueberspringen
        next_day += timedelta(days=1)
    return next_day.replace(hour=8, minute=0, second=0, microsecond=0)


# ═══════════════════════════════════════════════════════════════
#  MAIN SERVICE CLASS
# ═══════════════════════════════════════════════════════════════

class PresentationReplyService:
    """Klassifiziert und verarbeitet Kunden-Antworten auf Vorstellungs-E-Mails."""

    # ── 1. Reply zu Presentation matchen ──

    @staticmethod
    async def match_reply_to_presentation(
        db: AsyncSession,
        email_from: str,
        email_subject: str,
        in_reply_to_subject: str = "",
    ) -> Optional[dict]:
        """Findet die passende Presentation zu einer eingehenden Antwort.

        Matching-Strategie (Prioritaet):
        1. email_from == email_to einer aktiven Presentation
        2. Betreff-Matching (Re:/AW: entfernen, dann vergleichen)

        Returns:
            Dict mit Presentation-Daten oder None
        """
        try:
            from app.models.client_presentation import ClientPresentation
            from app.models.company import Company

            sender = email_from.strip().lower()

            # Strategie 1: E-Mail-Adresse matchen (zuverlaessigster Weg)
            result = await db.execute(
                select(
                    ClientPresentation.id,
                    ClientPresentation.candidate_id,
                    ClientPresentation.company_id,
                    ClientPresentation.contact_id,
                    ClientPresentation.email_to,
                    ClientPresentation.email_from,
                    ClientPresentation.email_subject,
                    ClientPresentation.mailbox_used,
                    ClientPresentation.status,
                    ClientPresentation.sequence_active,
                    ClientPresentation.sequence_step,
                    Company.name.label("company_name"),
                    Company.domain.label("company_domain"),
                ).outerjoin(
                    Company, Company.id == ClientPresentation.company_id
                ).where(
                    and_(
                        func.lower(ClientPresentation.email_to) == sender,
                        ClientPresentation.status.in_(["sent", "followup_1", "followup_2", "draft"]),
                    )
                ).order_by(
                    ClientPresentation.created_at.desc()
                ).limit(1)
            )
            row = result.first()

            # Strategie 2: Betreff-Matching als Fallback
            if not row and (email_subject or in_reply_to_subject):
                clean_subject = _clean_subject(in_reply_to_subject or email_subject)
                if clean_subject and len(clean_subject) > 10:
                    result = await db.execute(
                        select(
                            ClientPresentation.id,
                            ClientPresentation.candidate_id,
                            ClientPresentation.company_id,
                            ClientPresentation.contact_id,
                            ClientPresentation.email_to,
                            ClientPresentation.email_from,
                            ClientPresentation.email_subject,
                            ClientPresentation.mailbox_used,
                            ClientPresentation.status,
                            ClientPresentation.sequence_active,
                            ClientPresentation.sequence_step,
                            Company.name.label("company_name"),
                            Company.domain.label("company_domain"),
                        ).outerjoin(
                            Company, Company.id == ClientPresentation.company_id
                        ).where(
                            and_(
                                ClientPresentation.email_subject.ilike(f"%{clean_subject}%"),
                                ClientPresentation.status.in_(["sent", "followup_1", "followup_2", "draft"]),
                            )
                        ).order_by(
                            ClientPresentation.created_at.desc()
                        ).limit(1)
                    )
                    row = result.first()

            if not row:
                return None

            return {
                "id": str(row.id),
                "candidate_id": str(row.candidate_id) if row.candidate_id else None,
                "company_id": str(row.company_id) if row.company_id else None,
                "contact_id": str(row.contact_id) if row.contact_id else None,
                "email_to": row.email_to,
                "email_from": row.email_from,
                "email_subject": row.email_subject,
                "mailbox_used": row.mailbox_used,
                "status": row.status,
                "sequence_active": row.sequence_active,
                "sequence_step": row.sequence_step,
                "company_name": row.company_name,
                "company_domain": row.company_domain,
            }

        except Exception as e:
            logger.error(f"match_reply_to_presentation fehlgeschlagen: {e}")
            return None

    # ── 2. GPT-4o-mini Klassifikation ──

    @staticmethod
    async def classify_reply(
        email_subject: str,
        email_body: str,
        email_from: str,
    ) -> dict:
        """Klassifiziert eine Kunden-Antwort per GPT-4o-mini.

        WICHTIG: Keine DB-Session offen waehrend dieses Calls (Railway 30s Timeout).

        Returns:
            {"category": "POSITIVE|NEGATIVE|DELETION_REQUEST|AUTO_REPLY",
             "confidence": float, "reason": str}
        """
        try:
            import httpx
            from app.config import get_settings

            settings = get_settings()

            user_prompt = REPLY_CLASSIFIER_USER.format(
                subject=email_subject[:200],
                sender=email_from,
                body=email_body[:2000],  # Truncate — GPT braucht nicht den ganzen Roman
            )

            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {settings.openai_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "gpt-4o-mini",
                        "messages": [
                            {"role": "system", "content": REPLY_CLASSIFIER_SYSTEM},
                            {"role": "user", "content": user_prompt},
                        ],
                        "temperature": 0.1,  # Niedrig — Klassifikation soll deterministisch sein
                        "response_format": {"type": "json_object"},
                    },
                )
                response.raise_for_status()
                data = response.json()
                content = data["choices"][0]["message"]["content"]

            parsed = json.loads(content)

            # Validierung
            category = parsed.get("category", "AUTO_REPLY").upper()
            if category not in ("POSITIVE", "NEGATIVE", "DELETION_REQUEST", "AUTO_REPLY"):
                logger.warning(f"Unbekannte Kategorie von GPT: {category}, Fallback AUTO_REPLY")
                category = "AUTO_REPLY"

            confidence = max(0.0, min(1.0, float(parsed.get("confidence", 0.5))))
            reason = str(parsed.get("reason", ""))[:500]

            return {
                "category": category,
                "confidence": confidence,
                "reason": reason,
            }

        except Exception as e:
            logger.error(f"GPT Reply-Klassifikation fehlgeschlagen: {e}")
            # Fallback: Auto-Reply — im Zweifel nichts tun
            return {
                "category": "AUTO_REPLY",
                "confidence": 0.0,
                "reason": f"GPT-Fehler: {str(e)[:200]}",
            }

    # ── 3. Presentation aktualisieren + Sequenz stoppen ──

    @staticmethod
    async def stop_sequence_and_update(
        db: AsyncSession,
        presentation_id: str,
        reply_status: str,
        reply_body: str,
        classification: dict,
    ) -> bool:
        """Stoppt die Follow-Up-Sequenz und aktualisiert die Presentation.

        Args:
            presentation_id: UUID der Presentation
            reply_status: "replied_positive", "replied_negative", "deletion_requested"
            reply_body: Original-Antwort-Text (gekuerzt)
            classification: GPT-Klassifikations-Ergebnis
        """
        try:
            from app.models.client_presentation import ClientPresentation

            pres_id = UUID(presentation_id)
            now = datetime.now(timezone.utc)

            result = await db.execute(
                select(ClientPresentation).where(ClientPresentation.id == pres_id)
            )
            presentation = result.scalar_one_or_none()
            if not presentation:
                logger.warning(f"Presentation {presentation_id} nicht gefunden")
                return False

            # Sequenz stoppen
            presentation.sequence_active = False
            presentation.status = "responded"
            presentation.responded_at = now
            presentation.client_response_raw = reply_body[:5000]
            presentation.client_response_text = classification.get("reason", "")[:1000]

            # Reply-Tracking Felder (Migration 041)
            presentation.reply_status = reply_status
            presentation.reply_received_at = now
            presentation.reply_body_preview = reply_body[:500]

            # response_type Mapping
            category_to_response = {
                "replied_positive": "genuine_reply",
                "replied_negative": "genuine_reply",
                "deletion_requested": "genuine_reply",
            }
            presentation.response_type = category_to_response.get(reply_status, "genuine_reply")

            # client_response_category Mapping
            category_to_crm = {
                "replied_positive": "interesse_ja",
                "replied_negative": "kein_interesse",
                "deletion_requested": "kein_interesse",
            }
            presentation.client_response_category = category_to_crm.get(reply_status, "sonstiges")

            await db.commit()
            logger.info(
                f"Presentation {presentation_id} aktualisiert: "
                f"reply_status={reply_status}, sequence_active=False"
            )
            return True

        except Exception as e:
            logger.error(f"stop_sequence_and_update fehlgeschlagen: {e}")
            await db.rollback()
            return False

    # ── 4. GPT-4o-mini Reply generieren ──

    @staticmethod
    async def _generate_reply_text(
        reply_type: str,
        anrede: str,
        reply_body: str,
    ) -> str:
        """Generiert eine menschlich klingende Antwort per GPT-4o-mini.

        Args:
            reply_type: "negative" oder "deletion"
            anrede: z.B. "Hallo Frau Mueller" (aus Klassifikation)
            reply_body: Original-Nachricht des Kunden (Kontext)

        Returns:
            Generierter Antwort-Text (ohne Signatur)
        """
        try:
            import httpx
            from app.config import get_settings
            settings = get_settings()

            if reply_type == "deletion":
                user_prompt = REPLY_GENERATOR_DELETION.format(
                    anrede=anrede,
                    reply_body=reply_body[:500],
                )
            else:
                user_prompt = REPLY_GENERATOR_NEGATIVE.format(
                    anrede=anrede,
                    reply_body=reply_body[:500],
                )

            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {settings.openai_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "gpt-4o-mini",
                        "messages": [
                            {"role": "system", "content": REPLY_GENERATOR_SYSTEM},
                            {"role": "user", "content": user_prompt},
                        ],
                        "temperature": 0.8,  # Etwas Variation damit nicht jede Antwort gleich klingt
                        "max_tokens": 200,
                    },
                )
                response.raise_for_status()
                data = response.json()
                return data["choices"][0]["message"]["content"].strip()

        except Exception as e:
            logger.error(f"GPT Reply-Generierung fehlgeschlagen: {e}")
            # Fallback: Einfache Antwort mit Anrede
            if reply_type == "deletion":
                return f"{anrede},\n\nIhre Daten wurden aus unserem System geloescht. Sie erhalten keine weiteren Nachrichten von uns."
            else:
                return f"{anrede},\n\nvielen Dank fuer Ihre Rueckmeldung. Bei zukuenftigem Bedarf melden Sie sich gerne."

    # ── 5. Auto-Reply senden (NEGATIVE) ──

    @staticmethod
    async def send_negative_auto_reply(
        to_email: str,
        original_subject: str,
        mailbox_used: str,
        anrede: str = "Hallo",
        reply_body: str = "",
    ) -> dict:
        """Sendet eine freundliche Absage-Antwort per GPT-4o-mini.

        KEINE DB-Session offen waehrend des Sendens!
        Routing: sincirus.com -> Microsoft Graph, IONOS-Domains -> SMTP.
        """
        try:
            from app.services.ionos_smtp_client import IonosSmtpClient, is_ionos_mailbox

            # GPT-4o-mini generiert die Antwort mit korrekter Anrede
            body_text = await PresentationReplyService._generate_reply_text(
                reply_type="negative",
                anrede=anrede,
                reply_body=reply_body,
            )

            reply_subject = original_subject
            if not reply_subject.lower().startswith(("re:", "aw:")):
                reply_subject = f"Re: {reply_subject}"

            effective_from = mailbox_used or "hamdard@sincirus.com"

            if is_ionos_mailbox(effective_from):
                # IONOS SMTP Versand (sincirus-karriere.de, jobs-sincirus.com)
                from app.config import settings as app_settings
                plain_body = body_text + "\n\n--\n" + PLAIN_TEXT_SIGNATURE
                result = await IonosSmtpClient.send_email(
                    to_email=to_email,
                    subject=reply_subject,
                    body_plain=plain_body,
                    from_email=effective_from,
                    password=app_settings.ionos_smtp_password,
                )
            else:
                # Microsoft Graph Versand (sincirus.com)
                from app.services.email_service import MicrosoftGraphClient
                body_html = _format_reply_html(body_text)
                result = await MicrosoftGraphClient.send_email(
                    to_email=to_email,
                    subject=reply_subject,
                    body_html=body_html,
                    from_email=effective_from,
                )

            if result.get("success"):
                logger.info(f"Negative Auto-Reply gesendet an {to_email} von {effective_from}")
                return {"success": True, "reply_text": body_text, "error": None}
            else:
                error = result.get("error", "Unbekannt")
                logger.error(f"Negative Auto-Reply fehlgeschlagen: {error}")
                return {"success": False, "reply_text": body_text, "error": error}

        except Exception as e:
            logger.error(f"send_negative_auto_reply Exception: {e}")
            return {"success": False, "reply_text": "", "error": str(e)}

    # ── 6. Loeschbestaetigung senden (DELETION_REQUEST) ──

    @staticmethod
    async def send_deletion_confirmation(
        to_email: str,
        original_subject: str,
        anrede: str = "Hallo",
        reply_body: str = "",
        mailbox_used: str = "hamdard@sincirus.com",
    ) -> dict:
        """Sendet eine Loeschbestaetigung per GPT-4o-mini.

        KEINE DB-Session offen waehrend des Sendens!
        Routing: sincirus.com -> Microsoft Graph, IONOS-Domains -> SMTP.
        """
        try:
            from app.services.ionos_smtp_client import IonosSmtpClient, is_ionos_mailbox

            body_text = await PresentationReplyService._generate_reply_text(
                reply_type="deletion",
                anrede=anrede,
                reply_body=reply_body,
            )

            reply_subject = original_subject
            if not reply_subject.lower().startswith(("re:", "aw:")):
                reply_subject = f"Re: {reply_subject}"

            effective_from = mailbox_used or "hamdard@sincirus.com"

            if is_ionos_mailbox(effective_from):
                # IONOS SMTP Versand (sincirus-karriere.de, jobs-sincirus.com)
                from app.config import settings as app_settings
                plain_body = body_text + "\n\n--\n" + PLAIN_TEXT_SIGNATURE
                result = await IonosSmtpClient.send_email(
                    to_email=to_email,
                    subject=reply_subject,
                    body_plain=plain_body,
                    from_email=effective_from,
                    password=app_settings.ionos_smtp_password,
                )
            else:
                # Microsoft Graph Versand (sincirus.com)
                from app.services.email_service import MicrosoftGraphClient
                body_html = _format_reply_html(body_text)
                result = await MicrosoftGraphClient.send_email(
                    to_email=to_email,
                    subject=reply_subject,
                    body_html=body_html,
                    from_email=effective_from,
                )

            if result.get("success"):
                logger.info(f"Loeschbestaetigung gesendet an {to_email} von {effective_from}")
                return {"success": True, "reply_text": body_text, "error": None}
            else:
                error = result.get("error", "Unbekannt")
                logger.error(f"Loeschbestaetigung fehlgeschlagen: {error}")
                return {"success": False, "reply_text": body_text, "error": error}

        except Exception as e:
            logger.error(f"send_deletion_confirmation Exception: {e}")
            return {"success": False, "reply_text": "", "error": str(e)}

    # ── 6. GDPR Loeschung + Domain-Blocklist ──

    @staticmethod
    async def delete_company_and_blocklist(
        db: AsyncSession,
        company_id: str,
        email_domain: str,
        company_name: str,
        contact_email: str = "",
        reason: str = "DSGVO-Loeschungsanfrage via Reply-Monitor",
    ) -> dict:
        """Loescht alle Daten eines Unternehmens und blockiert die Domain.

        Reihenfolge (FK-Constraints beachten):
        1. Matches loeschen (FK auf jobs)
        2. Client Presentations loeschen (FK auf company, jobs, contacts)
        3. Jobs loeschen (FK auf company)
        4. Contacts loeschen (FK auf company, CASCADE)
        5. Company loeschen
        6. Domain zur Blocklist hinzufuegen

        Returns:
            {"success": bool, "deleted": dict, "error": str|None}
        """
        deleted = {
            "matches": 0,
            "presentations": 0,
            "jobs": 0,
            "contacts": 0,
            "company": False,
            "domain_blocked": False,
        }

        try:
            from app.models.client_presentation import ClientPresentation
            from app.models.company import Company
            from app.models.company_contact import CompanyContact
            from app.models.company_correspondence import CompanyCorrespondence
            from app.models.job import Job
            from app.models.match import Match
            from app.models.email_blocklist import EmailBlocklist

            comp_id = UUID(company_id)

            # 1. Job-IDs dieser Firma laden
            job_result = await db.execute(
                select(Job.id).where(Job.company_id == comp_id)
            )
            job_ids = [row[0] for row in job_result.all()]

            # 2. Matches loeschen (FK auf jobs)
            if job_ids:
                match_result = await db.execute(
                    delete(Match).where(Match.job_id.in_(job_ids))
                )
                deleted["matches"] = match_result.rowcount
                logger.info(f"GDPR: {deleted['matches']} Matches geloescht fuer Company {company_name}")

            # 3. Client Presentations loeschen
            pres_result = await db.execute(
                delete(ClientPresentation).where(ClientPresentation.company_id == comp_id)
            )
            deleted["presentations"] = pres_result.rowcount
            logger.info(f"GDPR: {deleted['presentations']} Presentations geloescht")

            # 4. CompanyCorrespondence loeschen (bevor Company/Contacts weg sind)
            await db.execute(
                delete(CompanyCorrespondence).where(CompanyCorrespondence.company_id == comp_id)
            )

            # 5. Jobs loeschen
            if job_ids:
                job_del_result = await db.execute(
                    delete(Job).where(Job.company_id == comp_id)
                )
                deleted["jobs"] = job_del_result.rowcount
                logger.info(f"GDPR: {deleted['jobs']} Jobs geloescht")

            # 6. Contacts loeschen
            contact_result = await db.execute(
                delete(CompanyContact).where(CompanyContact.company_id == comp_id)
            )
            deleted["contacts"] = contact_result.rowcount
            logger.info(f"GDPR: {deleted['contacts']} Contacts geloescht")

            # 7. Company loeschen
            company_result = await db.execute(
                delete(Company).where(Company.id == comp_id)
            )
            deleted["company"] = company_result.rowcount > 0
            logger.info(f"GDPR: Company '{company_name}' geloescht")

            # 8. Domain zur Blocklist hinzufuegen
            if email_domain:
                clean_domain = email_domain.strip().lower()
                # Pruefen ob schon vorhanden
                existing = await db.execute(
                    select(EmailBlocklist.id).where(
                        EmailBlocklist.domain == clean_domain
                    )
                )
                if not existing.scalar_one_or_none():
                    blocklist_entry = EmailBlocklist(
                        domain=clean_domain,
                        reason=reason,
                        company_name_before_deletion=company_name,
                        contact_email=contact_email,
                        blocked_by="auto_reply_monitor",
                    )
                    db.add(blocklist_entry)
                    deleted["domain_blocked"] = True
                    logger.info(f"GDPR: Domain '{clean_domain}' zur Blocklist hinzugefuegt")
                else:
                    deleted["domain_blocked"] = True  # War schon blockiert
                    logger.info(f"GDPR: Domain '{clean_domain}' war bereits blockiert")

            await db.commit()

            logger.info(
                f"GDPR-Loeschung komplett fuer '{company_name}': "
                f"matches={deleted['matches']}, presentations={deleted['presentations']}, "
                f"jobs={deleted['jobs']}, contacts={deleted['contacts']}, "
                f"domain_blocked={deleted['domain_blocked']}"
            )

            return {"success": True, "deleted": deleted, "error": None}

        except Exception as e:
            logger.error(f"GDPR-Loeschung fehlgeschlagen fuer {company_name}: {e}")
            await db.rollback()
            return {"success": False, "deleted": deleted, "error": str(e)}

    # ── 7. Domain-Blocklist pruefen ──

    @staticmethod
    async def is_domain_blocked(db: AsyncSession, email: str) -> bool:
        """Prueft ob eine E-Mail-Domain auf der Blocklist steht.

        Args:
            email: E-Mail-Adresse (z.B. "hr@example.de") oder Domain (z.B. "example.de")

        Returns:
            True wenn blockiert, False wenn nicht
        """
        try:
            from app.models.email_blocklist import EmailBlocklist

            # Domain extrahieren
            if "@" in email:
                domain = email.split("@", 1)[1].strip().lower()
            else:
                domain = email.strip().lower()

            if not domain:
                return False

            result = await db.execute(
                select(EmailBlocklist.id).where(
                    EmailBlocklist.domain == domain
                ).limit(1)
            )
            return result.scalar_one_or_none() is not None

        except Exception as e:
            logger.error(f"Blocklist-Check fehlgeschlagen fuer {email}: {e}")
            return False  # Im Fehlerfall nicht blockieren

    # ── 8. Auto-Reply in Presentation markieren ──

    @staticmethod
    async def mark_auto_reply_sent(
        db: AsyncSession,
        presentation_id: str,
        reply_text: str,
    ) -> bool:
        """Markiert dass eine Auto-Reply gesendet wurde."""
        try:
            from app.models.client_presentation import ClientPresentation

            pres_id = UUID(presentation_id)
            result = await db.execute(
                select(ClientPresentation).where(ClientPresentation.id == pres_id)
            )
            presentation = result.scalar_one_or_none()
            if not presentation:
                return False

            presentation.auto_reply_sent = True
            presentation.auto_reply_text = reply_text[:2000]
            await db.commit()
            return True

        except Exception as e:
            logger.error(f"mark_auto_reply_sent fehlgeschlagen: {e}")
            await db.rollback()
            return False

    # ── 9. Hauptmethode: Reply komplett verarbeiten ──

    @staticmethod
    async def process_reply(
        email_from: str,
        email_subject: str,
        email_body: str,
        in_reply_to_subject: str = "",
    ) -> dict:
        """Verarbeitet eine eingehende Antwort komplett.

        Wird von n8n aufgerufen. Nutzt eigene DB-Sessions pro Schritt
        (Railway 30s Timeout beachten).

        Ablauf:
        1. Reply zu Presentation matchen (eigene Session)
        2. GPT-Klassifikation (KEINE DB-Session offen!)
        3. Aktion ausfuehren je nach Kategorie (eigene Session)
        4. Auto-Reply senden wenn noetig (KEINE DB-Session offen!)
        5. Auto-Reply in DB markieren (eigene Session)

        Returns:
            {"matched": bool, "category": str, "action_taken": str, "details": dict}
        """
        result = {
            "matched": False,
            "category": None,
            "action_taken": "none",
            "presentation_id": None,
            "company_name": None,
            "details": {},
        }

        try:
            from app.database import async_session_maker

            # ── Schritt 1: Presentation matchen (eigene Session) ──
            presentation_data = None
            async with async_session_maker() as db:
                presentation_data = await PresentationReplyService.match_reply_to_presentation(
                    db=db,
                    email_from=email_from,
                    email_subject=email_subject,
                    in_reply_to_subject=in_reply_to_subject,
                )
            # Session geschlossen!

            if not presentation_data:
                logger.info(
                    f"Keine passende Presentation fuer Reply von {email_from} "
                    f"(Betreff: {email_subject[:80]})"
                )
                result["action_taken"] = "no_match"
                return result

            result["matched"] = True
            result["presentation_id"] = presentation_data["id"]
            result["company_name"] = presentation_data.get("company_name")

            # ── Schritt 2: GPT-Klassifikation (KEINE DB-Session!) ──
            classification = await PresentationReplyService.classify_reply(
                email_subject=email_subject,
                email_body=email_body,
                email_from=email_from,
            )

            category = classification["category"]
            confidence = classification.get("confidence", 0.0)

            # ── Confidence-Gate: Bei niedriger Sicherheit IMMER als POSITIVE behandeln ──
            # → Milad entscheidet manuell, kein automatisches Handeln
            if confidence < 0.80 and category in ("NEGATIVE", "DELETION_REQUEST"):
                original_category = category
                logger.warning(
                    f"Confidence-Gate: {original_category} mit confidence={confidence:.2f} "
                    f"von {email_from} → eskaliere als POSITIVE (Milad entscheidet)"
                )
                category = "POSITIVE"
                classification["original_category"] = original_category
                classification["category"] = "POSITIVE"
                classification["confidence_override"] = True
                classification["reason"] = (
                    f"[CONFIDENCE-GATE: Original={original_category}, "
                    f"Confidence={confidence:.2f}] {classification.get('reason', '')}"
                )

            anrede = classification.get("absender_anrede", "Hallo")
            result["category"] = category
            result["details"]["classification"] = classification
            result["details"]["anrede"] = anrede

            logger.info(
                f"Reply klassifiziert: {category} "
                f"(Confidence: {classification['confidence']:.2f}) "
                f"von {email_from} fuer Presentation {presentation_data['id']}"
            )

            # ── AUTO_REPLY: Komplett ignorieren ──
            if category == "AUTO_REPLY":
                result["action_taken"] = "ignored_auto_reply"
                logger.info(f"Auto-Reply ignoriert von {email_from}")
                return result

            # ── Schritt 3: Sequenz stoppen + Presentation aktualisieren (eigene Session) ──
            reply_status_map = {
                "POSITIVE": "replied_positive",
                "NEGATIVE": "replied_negative",
                "DELETION_REQUEST": "deletion_requested",
            }
            reply_status = reply_status_map.get(category, "replied_positive")

            async with async_session_maker() as db:
                updated = await PresentationReplyService.stop_sequence_and_update(
                    db=db,
                    presentation_id=presentation_data["id"],
                    reply_status=reply_status,
                    reply_body=email_body,
                    classification=classification,
                )
            # Session geschlossen!

            if not updated:
                logger.error(f"Konnte Presentation {presentation_data['id']} nicht aktualisieren")
                result["action_taken"] = "update_failed"
                return result

            # ── POSITIVE: Sequenz gestoppt, Milad antwortet manuell ──
            if category == "POSITIVE":
                result["action_taken"] = "sequence_stopped_positive"
                logger.info(
                    f"POSITIVE Reply von {email_from} — "
                    f"Sequenz gestoppt, Milad antwortet manuell"
                )
                return result

            # ── NEGATIVE: Auto-Reply senden (KEINE DB-Session!) ──
            if category == "NEGATIVE":
                if _is_business_hours():
                    # Innerhalb Geschaeftszeiten → sofort senden
                    send_result = await PresentationReplyService.send_negative_auto_reply(
                        to_email=email_from,
                        original_subject=email_subject,
                        mailbox_used=presentation_data.get("mailbox_used", "hamdard@sincirus.com"),
                        anrede=anrede,
                        reply_body=email_body,
                    )
                    result["details"]["auto_reply"] = send_result

                    if send_result.get("success"):
                        # Auto-Reply in DB markieren (eigene Session)
                        reply_text = send_result.get("reply_text", "")

                        async with async_session_maker() as db:
                            await PresentationReplyService.mark_auto_reply_sent(
                                db=db,
                                presentation_id=presentation_data["id"],
                                reply_text=reply_text,
                            )
                        # Session geschlossen!

                        result["action_taken"] = "sequence_stopped_negative_replied"
                    else:
                        result["action_taken"] = "sequence_stopped_negative_reply_failed"
                else:
                    # Ausserhalb Geschaeftszeiten → verzoegern bis naechster Werktag 8:00
                    scheduled_at = _next_business_morning()
                    async with async_session_maker() as db:
                        from app.models.client_presentation import ClientPresentation
                        pres_id_uuid = UUID(presentation_data["id"])
                        pres_result = await db.execute(
                            select(ClientPresentation).where(ClientPresentation.id == pres_id_uuid)
                        )
                        pres = pres_result.scalar_one_or_none()
                        if pres:
                            # Pending-Info in client_response_text speichern (JSONB-artig, kein Migration noetig)
                            pending_data = {
                                "auto_reply_pending": True,
                                "auto_reply_scheduled_at": scheduled_at.astimezone(timezone.utc).isoformat(),
                                "auto_reply_type": "negative",
                                "auto_reply_to": email_from,
                                "auto_reply_subject": email_subject,
                                "auto_reply_anrede": anrede,
                                "auto_reply_body_context": email_body[:500],
                                "mailbox_used": presentation_data.get("mailbox_used", "hamdard@sincirus.com"),
                            }
                            pres.client_response_text = json.dumps(pending_data)
                            await db.commit()
                    # Session geschlossen!

                    result["action_taken"] = "sequence_stopped_negative_reply_scheduled"
                    result["details"]["scheduled_at"] = scheduled_at.isoformat()
                    logger.info(
                        f"NEGATIVE Auto-Reply fuer {email_from} verzoegert bis "
                        f"{scheduled_at.isoformat()} (ausserhalb Geschaeftszeiten)"
                    )

                return result

            # ── DELETION_REQUEST: Alles loeschen + Bestaetigung senden ──
            if category == "DELETION_REQUEST":
                company_id = presentation_data.get("company_id")
                company_name = presentation_data.get("company_name", "Unbekannt")
                company_domain = presentation_data.get("company_domain", "")

                # E-Mail-Domain extrahieren
                email_domain = ""
                if "@" in email_from:
                    email_domain = email_from.split("@", 1)[1].strip().lower()
                elif company_domain:
                    email_domain = company_domain

                if company_id:
                    # GDPR-Loeschung (eigene Session)
                    async with async_session_maker() as db:
                        deletion_result = await PresentationReplyService.delete_company_and_blocklist(
                            db=db,
                            company_id=company_id,
                            email_domain=email_domain,
                            company_name=company_name,
                            contact_email=email_from,
                        )
                    # Session geschlossen!

                    result["details"]["deletion"] = deletion_result

                    if deletion_result.get("success"):
                        logger.info(f"GDPR-Loeschung erfolgreich fuer {company_name}")
                    else:
                        logger.error(
                            f"GDPR-Loeschung fehlgeschlagen fuer {company_name}: "
                            f"{deletion_result.get('error')}"
                        )
                else:
                    logger.warning(f"DELETION_REQUEST aber keine company_id fuer {email_from}")
                    # Domain trotzdem blocklisten
                    if email_domain:
                        async with async_session_maker() as db:
                            from app.models.email_blocklist import EmailBlocklist
                            existing = await db.execute(
                                select(EmailBlocklist.id).where(
                                    EmailBlocklist.domain == email_domain
                                )
                            )
                            if not existing.scalar_one_or_none():
                                db.add(EmailBlocklist(
                                    domain=email_domain,
                                    reason="DSGVO-Anfrage (keine Company zugeordnet)",
                                    contact_email=email_from,
                                    blocked_by="auto_reply_monitor",
                                ))
                                await db.commit()
                        # Session geschlossen!

                # Loeschbestaetigung senden — DSGVO-Loeschung ist SOFORT,
                # aber Bestaetigungs-E-Mail nur in Geschaeftszeiten
                if _is_business_hours():
                    confirm_result = await PresentationReplyService.send_deletion_confirmation(
                        to_email=email_from,
                        original_subject=email_subject,
                        anrede=anrede,
                        reply_body=email_body,
                        mailbox_used=presentation_data.get("mailbox_used", "hamdard@sincirus.com"),
                    )
                    result["details"]["confirmation_email"] = confirm_result
                    result["action_taken"] = "deletion_completed" if confirm_result.get("success") else "deletion_done_email_failed"
                else:
                    # Bestaetigungs-E-Mail verzoegern (Loeschung ist bereits passiert!)
                    scheduled_at = _next_business_morning()
                    # Da Company bereits geloescht ist, Pending-Info im Result speichern
                    # und in einer noch existierenden Presentation (falls vorhanden) markieren
                    try:
                        async with async_session_maker() as db:
                            from app.models.client_presentation import ClientPresentation
                            pres_id_uuid = UUID(presentation_data["id"])
                            pres_result = await db.execute(
                                select(ClientPresentation).where(ClientPresentation.id == pres_id_uuid)
                            )
                            pres = pres_result.scalar_one_or_none()
                            if pres:
                                pending_data = {
                                    "auto_reply_pending": True,
                                    "auto_reply_scheduled_at": scheduled_at.astimezone(timezone.utc).isoformat(),
                                    "auto_reply_type": "deletion_confirmation",
                                    "auto_reply_to": email_from,
                                    "auto_reply_subject": email_subject,
                                    "auto_reply_anrede": anrede,
                                    "auto_reply_body_context": email_body[:500],
                                }
                                pres.client_response_text = json.dumps(pending_data)
                                await db.commit()
                        # Session geschlossen!
                    except Exception as sched_err:
                        # Presentation koennte durch GDPR-Loeschung bereits weg sein
                        logger.warning(
                            f"Konnte Pending-Info nicht speichern (Presentation evtl. geloescht): {sched_err}"
                        )

                    result["action_taken"] = "deletion_done_email_scheduled"
                    result["details"]["scheduled_at"] = scheduled_at.isoformat()
                    logger.info(
                        f"DELETION Bestaetigungs-E-Mail fuer {email_from} verzoegert bis "
                        f"{scheduled_at.isoformat()} (ausserhalb Geschaeftszeiten, Loeschung bereits erfolgt)"
                    )

                return result

        except Exception as e:
            logger.error(f"process_reply komplett fehlgeschlagen: {e}", exc_info=True)
            result["action_taken"] = "error"
            result["details"]["error"] = str(e)[:500]
            return result


# ═══════════════════════════════════════════════════════════════
#  HELPER-FUNKTIONEN
# ═══════════════════════════════════════════════════════════════

def _clean_subject(subject: str) -> str:
    """Entfernt Re:/AW:/Fwd: Praefixe und Whitespace aus Betreff.

    Beispiel: "AW: Re: Kandidat fuer Bilanzbuchhalter" → "Kandidat fuer Bilanzbuchhalter"
    """
    if not subject:
        return ""

    import re
    cleaned = subject.strip()
    # Iterativ Prefixe entfernen
    pattern = re.compile(r'^(Re:|AW:|Fwd:|WG:|FW:)\s*', re.IGNORECASE)
    while pattern.match(cleaned):
        cleaned = pattern.sub('', cleaned).strip()
    return cleaned


def _format_reply_html(body_text: str) -> str:
    """Formatiert Plain-Text Body + Signatur als HTML fuer Microsoft Graph.

    Einfaches HTML — keine aufwaendige Formatierung, soll natuerlich aussehen.
    """
    # Zeilenumbrueche in <br> umwandeln
    body_html = body_text.replace("\n", "<br>")
    signature_html = PLAIN_TEXT_SIGNATURE.replace("\n", "<br>")

    return f"""<div style="font-family: Calibri, Arial, sans-serif; font-size: 14px; color: #333;">
<p>{body_html}</p>
<p style="margin-top: 24px; color: #555; font-size: 12px; border-top: 1px solid #ddd; padding-top: 12px;">
{signature_html}
</p>
</div>"""
