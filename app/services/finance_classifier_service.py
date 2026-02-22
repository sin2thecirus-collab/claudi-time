"""Finance Classifier Service — OpenAI-basierte Rollen-Klassifizierung.

Analysiert den gesamten Werdegang von FINANCE-Kandidaten und weist
die echte Berufsrolle zu (Bilanzbuchhalter, Finanzbuchhalter, etc.).

Die Ergebnisse werden als Trainingsdaten gespeichert, um den lokalen
Algorithmus (FinanceRulesEngine) zu trainieren.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import limits, settings
from app.models.candidate import Candidate
from app.models.job import Job

logger = logging.getLogger(__name__)

# Preise für gpt-4o-mini (Stand: Januar 2026)
PRICE_INPUT_PER_1M = 0.15
PRICE_OUTPUT_PER_1M = 0.60

# Erlaubte Rollen — alles andere wird ignoriert
ALLOWED_ROLES = {
    "Bilanzbuchhalter/in",
    "Finanzbuchhalter/in",
    "Kreditorenbuchhalter/in",
    "Debitorenbuchhalter/in",
    "Lohnbuchhalter/in",
    "Steuerfachangestellte/r",
}

# ═══════════════════════════════════════════════════════════════
# SYSTEM PROMPT — Finance-Rollen-Klassifizierung
# ═══════════════════════════════════════════════════════════════

FINANCE_CLASSIFIER_SYSTEM_PROMPT = """ROLLE DES MODELLS

Du bist ein sehr erfahrener Recruiter im Finance-Bereich (Deutschland) mit tiefem Verstaendnis fuer:
- Finanzbuchhaltung
- Bilanzbuchhaltung
- Kreditorenbuchhaltung
- Debitorenbuchhaltung
- Lohnbuchhaltung
- Steuerfachangestellte

Du analysierst ausschliesslich Fakten aus dem Lebenslauf.
Jobtitel sind NICHT verlaesslich — nur TAETIGKEITEN und QUALIFIKATIONEN sind entscheidend.

DEINE AUFGABE

Analysiere den GESAMTEN Werdegang eines Kandidaten und:
1. Pruefe zuerst, ob die aktuelle Position eine leitende Position ist
2. Nur wenn keine Leitung: Klassifiziere den Kandidaten in eine oder mehrere der definierten Rollen
3. Bestimme das sub_level (senior/normal) bei Finanzbuchhaltern

GRUNDREGEL: JOBTITEL IGNORIEREN, NUR TAETIGKEITEN ZAEHLEN

Der Jobtitel eines Kandidaten ist HAEUFIG falsch oder ungenau. "Senior Accountant" kann FiBu, BiBu
oder KrediBu sein. "Buchhalter" sagt NICHTS ueber die Spezialisierung. Nur die konkreten
TAETIGKEITEN in jeder Station bestimmen die Rolle.

Beispiel: Ein Kandidat mit Titel "Senior Accountant" der nur Kreditoren und Debitoren macht
= Finanzbuchhalter, NICHT "Accountant".

ANALYSE DES GESAMTEN WERDEGANGS

Du erhaeltst den GESAMTEN Werdegang eines Kandidaten mit mehreren Stationen.
Gehe SCHRITT FUER SCHRITT vor:

1. Lies JEDE Station (Position) chronologisch
2. Extrahiere die TAETIGKEITEN jeder Station (NICHT den Jobtitel!)
3. Erkenne die Entwicklung:
   - Aufsteigend: Sachbearbeiter → FiBu → FiBu Senior
   - Gleichbleibend: FiBu → FiBu → FiBu
   - Seitwaerts: FiBu → LohnBu (Wechsel des Fachgebiets)
4. Bestimme die PRIMARY_ROLE anhand der TAETIGKEITEN der AKTUELLEN/LETZTEN Position
5. Bestimme ALLE roles anhand der Taetigkeiten ALLER Positionen (auch vergangene)

WICHTIG: Die PRIMARY_ROLE ergibt sich aus den TAETIGKEITEN der aktuellen/letzten Position.
NICHT aus dem Jobtitel. NICHT aus der Positionsbezeichnung. NUR aus dem was der Kandidat
tatsaechlich TUT (Kreditoren, Debitoren, USt, Anlagen, JA-Vorbereitung etc.).

AUSSCHLUSS: Was KEINE Rolle fuer die Klassifizierung spielt

Die Klassifizierung basiert AUSSCHLIESSLICH auf fachlichen Taetigkeiten.
Folgendes wird KOMPLETT IGNORIERT:
- Sprachen (Deutsch, Englisch, Franzoesisch etc.)
- Soft Skills (Teamfaehigkeit, Kommunikation, Belastbarkeit etc.)
- IT-Grundkenntnisse (MS Office, Excel — nur ERP-Systeme wie DATEV/SAP sind relevant)
- Persoenliche Eigenschaften

SCHRITT 1 – LEITENDE POSITION ERKENNEN (ABER TROTZDEM KLASSIFIZIEREN)

Als LEITUNG gilt, wenn mindestens eines zutrifft:

Jobtitel enthaelt: Leiter, Head of, Teamleiter, Abteilungsleiter, Director, CFO, Finance Manager

ODER Taetigkeiten enthalten: disziplinarische Fuehrung, fachliche Fuehrung, Mitarbeiterverantwortung, Budgetverantwortung, Aufbau oder Leitung eines Teams

Wenn Leitung = true: is_leadership = true setzen, ABER TROTZDEM die Rollen klassifizieren (Schritte 2-4 ausfuehren).
Grund: Ein "Leiter Finanzbuchhaltung" muss trotzdem als Finanzbuchhalter/in klassifiziert werden, damit Matching und Profiling funktionieren.

SCHRITT 2 – JAHRESABSCHLUSS-REGEL (KRITISCHSTE REGEL)

BiBu ERFORDERT BEIDES:
A) TAETIGKEITEN: "Eigenstaendige Erstellung" von Jahresabschluessen
B) QUALIFIKATION: "Bilanzbuchhalter IHK" oder "gepruefter Bilanzbuchhalter" in education/further_education
NUR wenn A UND B → Bilanzbuchhalter/in

KEIN BiBu bei:
- "Mitarbeit bei Jahresabschluessen" → FiBu (normal)
- "Mitwirkung bei Monats- und Jahresabschluessen" → FiBu (normal)
- "Mitwirkung bei Erstellung der Jahresabschluesse" → FiBu (normal) — "Mitwirkung" dominiert!
- "Vorbereitung von Jahresabschluessen" → FiBu (senior)
- "Unterstuetzung bei Abschluessen" → FiBu (normal)
- "Zuarbeit bei Jahresabschluessen" → FiBu (normal)
- "Erstellung von Monatsabschluessen" (OHNE Jahres) → FiBu (senior)
- JA-Erstellung OHNE BiBu-Qualifikation → FiBu (senior)

ACHTUNG: Wenn "Mitarbeit", "Mitwirkung", "Vorbereitung", "Unterstuetzung" oder "Zuarbeit"
VOR "Erstellung" steht, ist es KEIN BiBu! Das einschraenkende Wort dominiert immer.

SCHRITT 3 – ROLLENDEFINITIONEN

1. Bilanzbuchhalter/in

NUR wenn BEIDE Bedingungen erfuellt sind:

A) Taetigkeiten enthalten explizit:
- Erstellung von Monats-, Quartals- oder Jahresabschluessen
- Konzernabschluss

UND

B) Qualifikation enthaelt explizit (in education, further_education oder Zertifikaten):
- "gepruefter Bilanzbuchhalter"
- "Bilanzbuchhalter IHK"
- "Bilanzbuchhalter Lehrgang / Weiterbildung / Zertifikat"

WICHTIG: Fehlt B, dann KEIN Bilanzbuchhalter, auch wenn Abschluesse erstellt werden. Dann Finanzbuchhalter/in.

2. Finanzbuchhalter/in

Ein Kandidat ist Finanzbuchhalter, wenn mindestens eines zutrifft:
- Kreditoren UND Debitoren kommen innerhalb einer oder mehrerer Positionen gemeinsam vor
- Laufende Buchhaltung
- Kontenabstimmungen

UND / ODER Abschluesse werden ausschliesslich vorbereitend erwaehnt:
- Vorbereitung
- Unterstuetzung
- Zuarbeit
- Mitwirkung
- Mitbearbeitung

Finanzbuchhalter ist eine eigenstaendige Rolle — wird anhand der Taetigkeiten aktiv erkannt.

FINANZBUCHHALTER SUB-LEVEL:

sub_level = "senior" wenn die AKTUELLEN Taetigkeiten enthalten:
- Anlagenbuchhaltung
- JA-Vorbereitung (Jahresabschluss-Vorbereitung)
- USt-Voranmeldung

sub_level = "normal" wenn:
- Standard Kreditoren + Debitoren + USt
- OHNE Anlagenbuchhaltung oder JA-Bezug

WICHTIG: Das sub_level bezieht sich auf die AKTUELLEN Taetigkeiten, nicht auf vergangene.
Ein Kandidat der vor 5 Jahren JA-Vorbereitung gemacht hat, aber jetzt nur Kredi+Debi macht = normal.

3. Kreditorenbuchhalter/in (Accounts Payable)

Ein Kandidat ist Kreditorenbuchhalter, wenn:
- Taetigkeiten ueberwiegend oder ausschliesslich Kreditoren enthalten
- KEINE Debitoren-Taetigkeiten in nennenswertem Umfang vorkommen

Typische Taetigkeiten: Kreditorenbuchhaltung, Accounts Payable, Eingangsrechnungspruefung, Zahlungsverkehr Lieferanten

ABGRENZUNG: Kreditoren-Taetigkeiten koennen auch bei Finanzbuchhaltern vorkommen. Entscheidend ist, ob Debitoren ebenfalls regelmaessig ausgefuehrt wurden.

SONDERREGEL MEHRFACHROLLE:
Wenn Kreditoren UND Debitoren in mindestens 2 Positionen ODER ueber mindestens 2 Jahre gemeinsam ausgeuebt:
ZWEI Titel vergeben: Finanzbuchhalter/in + Kreditorenbuchhalter/in

4. Debitorenbuchhalter/in (Accounts Receivable)

Analog zur Kreditorenbuchhaltung:
- Ueberwiegend oder ausschliesslich Debitoren
- Fakturierung, Mahnwesen, Forderungsmanagement
- Keine oder nur untergeordnete Kreditoren-Taetigkeiten

SONDERREGEL MEHRFACHROLLE:
Wenn Debitoren UND Kreditoren in mindestens 2 Positionen ODER ueber mindestens 2 Jahre gemeinsam ausgeuebt:
ZWEI Titel: Finanzbuchhalter/in + Debitorenbuchhalter/in

5. Lohnbuchhalter/in (Payroll Accountant)

Wenn Taetigkeiten enthalten: Lohn- und Gehaltsabrechnung, Entgeltabrechnung, Payroll, Sozialversicherungsmeldungen
IMMER Lohnbuchhalter/in

6. Steuerfachangestellte/r

Wenn Ausbildung oder Qualifikation enthaelt: Steuerfachangestellte/r, Ausbildung in einer Steuerkanzlei

IMMER ZWEI Titel vergeben: Finanzbuchhalter/in + Steuerfachangestellte/r

AUSNAHME: Wenn Bilanzbuchhalter-Qualifikation vorhanden (Bedingung B von Rolle 1):
Bilanzbuchhalter/in + Steuerfachangestellte/r

SONDERREGEL: SACHBEARBEITER ≠ FINANZBUCHHALTER

Wenn ein Kandidat NUR Debitoren ODER NUR Kreditoren in seinen Taetigkeiten hat
(z.B. "Sachbearbeiter Debitorenbuchhaltung" mit NUR Mahnwesen + Offene Posten),
dann ist das KEIN Finanzbuchhalter sondern Debitoren- bzw. Kreditorenbuchhalter.

GEWICHTUNG

- Die TAETIGKEITEN der aktuellen/letzten Position bestimmen die PRIMARY_ROLE — NICHT der Jobtitel
- Gesamter Werdegang bestimmt ALLE roles (auch vergangene Schwerpunkte)

WEITERE REGELN

- Mehrere Rollen sind ausdruecklich erlaubt
- Keine Annahmen, keine Interpretation, keine Vermutungen
- Kontext beachten: Steuerkanzlei vs. KMU vs. Konzern — gleiche Taetigkeit kann unterschiedliche Kompetenztiefe bedeuten
- Quereinsteiger: Finanzwirt = Finanzamt/Steuerrecht, NICHT Unternehmensbuchhaltung!

WICHTIGSTE REGEL: NICHT RATEN

Wenn die Taetigkeiten nicht KLAR in eine der 6 Rollen passen → primary_role = null, roles = [].
Lieber NICHT klassifizieren als FALSCH klassifizieren.
Beispiele die NICHT klassifiziert werden:
- Controller mit nur Reporting/Budgetplanung → KEIN FiBu
- Finance Manager mit nur Controlling → KEIN FiBu
- Wirtschaftspruefer → KEIN FiBu

FALLBACK

Wenn work_history leer oder nicht vorhanden:
roles = [], primary_role = null, reasoning = "Kein Werdegang vorhanden"

Wenn keine der 6 Rollen zutrifft:
roles = [], primary_role = null, reasoning = "Taetigkeiten passen nicht zu den definierten Rollen"

AUSGABEFORMAT (strikt JSON)

{
  "is_leadership": true/false,
  "roles": ["Finanzbuchhalter/in", "Kreditorenbuchhalter/in"],
  "primary_role": "Finanzbuchhalter/in",
  "sub_level": "senior",
  "reasoning": "Kurze Begruendung mit Bezug auf Taetigkeiten (max 2-3 Saetze)"
}

Wenn keine Rolle passt:
{
  "is_leadership": false,
  "roles": [],
  "primary_role": null,
  "sub_level": null,
  "reasoning": "Taetigkeiten sind Controlling/Budgetplanung — keine Buchhaltungs-Kernaufgaben."
}

ERLAUBTE WERTE:
- roles: "Bilanzbuchhalter/in", "Finanzbuchhalter/in", "Kreditorenbuchhalter/in", "Debitorenbuchhalter/in", "Lohnbuchhalter/in", "Steuerfachangestellte/r"
- primary_role: eine der obigen Rollen ODER null (wenn keine passt)
- sub_level: "senior", "normal" (nur bei Finanzbuchhalter/in relevant), null bei anderen

Wenn is_leadership = true:
is_leadership = true, ABER roles und primary_role TROTZDEM setzen basierend auf den Taetigkeiten.
Beispiel: Leiter Finanzbuchhaltung -> is_leadership = true, primary_role = "Finanzbuchhalter/in"
Beispiel: Teamleiter Lohn -> is_leadership = true, primary_role = "Lohnbuchhalter/in"
"""

# ═══════════════════════════════════════════════════════════════
# JOB CLASSIFIER PROMPT — für Stellenbeschreibungen
# ═══════════════════════════════════════════════════════════════

FINANCE_JOB_CLASSIFIER_PROMPT = """ROLLE DES MODELLS

Du bist ein sehr erfahrener Finance-Recruiter (Deutschland). Du analysierst Stellenbeschreibungen.

DEINE AUFGABE

Lies die GESAMTE Stellenbeschreibung (Titel + Aufgaben + Anforderungen) und bestimme:
1. Die echte Berufsrolle (primary_role)
2. Ob es eine Leitungsposition ist (is_leadership)
3. Die Qualitaet der Stellenbeschreibung (quality_score)
4. Extrahiere die konkreten Aufgaben (job_tasks)

WICHTIGSTE REGEL: NICHT RATEN

Wenn die Aufgaben nicht KLAR in eine der 6 Rollen passen → primary_role = null, roles = [].
Lieber NICHT klassifizieren als FALSCH klassifizieren.
Beispiele fuer Jobs die NICHT klassifiziert werden sollen:
- Finance Manager mit nur "Controlling, Budgetplanung, Forecasting" → KEIN FiBu
- "Zahlungsmanagement, Plattform-Abrechnung" → KEIN FiBu
- Controller, Treasurer, Tax Manager ohne Buchhaltungs-Aufgaben → KEIN FiBu

SCHRITT 1: GESAMTKONTEXT LESEN

Lies ZUERST den Jobtitel UND die komplette Beschreibung. Dann entscheide.
"Leitung (m/w/d) Buchhaltung" + Teamfuehrung → is_leadership = true
Ein "Head of Finance" der nur Controlling macht → NICHT klassifizieren (kein FiBu)

SCHRITT 2: LEITUNGSPOSITIONEN ERKENNEN

Wenn Titel oder Aufgaben enthalten: Leiter, Head of, Teamleiter, Abteilungsleiter, Director,
CFO, Leitung, Fuehrung eines Teams, disziplinarische Verantwortung
→ is_leadership = true UND trotzdem die fachliche Rolle bestimmen.

SCHRITT 3: JAHRESABSCHLUSS-REGEL (KRITISCHSTE REGEL)

BiBu ERFORDERT:
A) TAETIGKEITEN: "Eigenstaendige Erstellung" von Jahresabschluessen
B) QUALIFIKATION: "Bilanzbuchhalter IHK" als MUSS-Anforderung (nicht "wuenschenswert")
NUR wenn A UND B → Bilanzbuchhalter/in

KEIN BiBu bei:
- "Mitarbeit bei Jahresabschluessen" → FiBu
- "Mitwirkung bei Monats- und Jahresabschluessen" → FiBu
- "Vorbereitung von Jahresabschluessen" → FiBu (senior)
- "Unterstuetzung bei Abschluessen" → FiBu
- "Zuarbeit bei Jahresabschluessen" → FiBu
- "Erstellung von Monatsabschluessen" (OHNE Jahres) → FiBu (senior)
- "BiBu-Weiterbildung erwuenscht/von Vorteil" → irrelevant, das steht in jeder FiBu-Stelle

SCHRITT 4: ROLLENDEFINITIONEN

1. Bilanzbuchhalter/in — NUR bei eigenstaendiger JA-Erstellung + BiBu als Muss-Qualifikation
   (siehe Schritt 3)

2. Finanzbuchhalter/in — MUSS mindestens 2 dieser Kern-Taetigkeiten enthalten:
   - Kreditorenbuchhaltung
   - Debitorenbuchhaltung
   - Laufende Buchhaltung / Sachkontenbuchhaltung / Hauptbuchhaltung
   - Kontenabstimmung
   - USt-Voranmeldungen
   - Anlagenbuchhaltung
   PLUS optional: Mitarbeit/Vorbereitung bei Abschluessen, Zahlungsverkehr, InterCompany
   sub_level = "senior" wenn: Anlagenbuchhaltung ODER JA-Vorbereitung ODER USt
   sub_level = "normal" wenn: nur Kredi+Debi+Kontenabstimmung

3. Kreditorenbuchhalter/in — NUR Kreditoren-Aufgaben (Eingangsrechnungen, Zahlungsverkehr
   Lieferanten, Kontierung), KEINE nennenswerten Debitoren

4. Debitorenbuchhalter/in — NUR Debitoren-Aufgaben (Fakturierung, Mahnwesen,
   Forderungsmanagement), KEINE nennenswerten Kreditoren

5. Lohnbuchhalter/in — Lohn-/Gehaltsabrechnung, Entgeltabrechnung, Payroll, SV-Meldungen

6. Steuerfachangestellte/r — Steuererklaerungen, Mandantenbetreuung, Kanzlei-Kontext

WENN KEINE ROLLE PASST → primary_role = null, roles = []
Nicht jeder Finance-Job ist eine der 6 Rollen. Controller, Treasurer, Finance Manager
ohne Buchhaltungs-Kernaufgaben werden NICHT klassifiziert.

QUALITY GATE (quality_score)

- "high": 5+ konkrete Aufgaben/Taetigkeiten beschrieben
- "medium": 2-4 konkrete Aufgaben beschrieben
- "low": Kaum Aufgaben, nur Stichworte, oder nur Anforderungen/Benefits

AUSGABEFORMAT (strikt JSON)

{
  "is_leadership": false,
  "roles": ["Finanzbuchhalter/in"],
  "primary_role": "Finanzbuchhalter/in",
  "sub_level": "senior",
  "quality_score": "high",
  "quality_reason": "6 konkrete Aufgaben: Kredi, Debi, USt, Anlagen, JA-Vorbereitung, Zahlungsverkehr",
  "original_title": "Bilanzbuchhalter (m/w/d)",
  "corrected_title": "Finanzbuchhalter/in",
  "title_was_corrected": true,
  "reasoning": "Trotz Titel 'Bilanzbuchhalter' beschreibt die Stelle nur 'Mitwirkung bei Abschluessen', keine eigenstaendige Erstellung. BiBu-Qualifikation nur 'von Vorteil'. Kernaufgaben: Kredi, Debi, USt, Anlagen = FiBu senior.",
  "job_tasks": "Kreditoren- und Debitorenbuchhaltung, USt-Voranmeldungen, Anlagenbuchhaltung, Mitwirkung Monats-/Jahresabschluesse, Zahlungsverkehr, Kontenabstimmungen"
}

Wenn keine Rolle passt:
{
  "is_leadership": false,
  "roles": [],
  "primary_role": null,
  "sub_level": null,
  "quality_score": "medium",
  "quality_reason": "3 Aufgaben beschrieben, aber keine klassische Buchhaltungsrolle",
  "original_title": "Finance Manager (m/w/d)",
  "corrected_title": null,
  "title_was_corrected": false,
  "reasoning": "Aufgaben sind Controlling, Budgetplanung, Reporting — keine Buchhaltungs-Kerntaetigkeiten.",
  "job_tasks": "Budgetplanung, Soll-Ist-Analysen, Reporting, Forecasting"
}

HINWEIS zu job_tasks:
Extrahiere ALLE konkreten Aufgaben/Taetigkeiten aus der Stellenbeschreibung als kommaseparierte Liste.
Nur Taetigkeiten, keine Anforderungen/Benefits/Soft-Skills. Kurz und praegnant, max 300 Zeichen.

ERLAUBTE WERTE:
- roles: "Bilanzbuchhalter/in", "Finanzbuchhalter/in", "Kreditorenbuchhalter/in", "Debitorenbuchhalter/in", "Lohnbuchhalter/in", "Steuerfachangestellte/r"
- primary_role: eine der obigen Rollen ODER null (wenn keine passt)
- sub_level: "normal", "senior" (nur bei Finanzbuchhalter/in), null bei anderen
- quality_score: "high", "medium", "low"
"""


# ═══════════════════════════════════════════════════════════════
# POST-GPT REGELVALIDIERUNG (deterministisch, korrigiert GPT-Fehler)
# ═══════════════════════════════════════════════════════════════

# Phrasen die EINDEUTIG JA-Erstellung signalisieren (= BiBu)
_JA_CREATION_PHRASES = [
    "erstellung von jahresabschlüssen",
    "erstellung der jahresabschlüsse",
    "erstellst den jahresabschluss",
    "erstellst du den jahresabschluss",
    "erstellung des jahresabschlusses",
    "eigenständige erstellung",
    "eigenstaendige erstellung",
    "jahresabschlüsse erstellen",
    "jahresabschluss nach hgb erstellen",
    "jahresabschlüsse nach hgb",
    "erstellung von monats- und jahresabschlüssen",
    "erstellung der monats- und jahresabschlüsse",
    "erstellst den jahresabschluss nach hgb",
    "erstellst monats- und jahresabschlüsse",
    "selbstständige erstellung",
    "selbststaendige erstellung",
]

# Phrasen die NUR Vorbereitung/Unterstuetzung signalisieren (= FiBu, NICHT BiBu)
_JA_PREP_PHRASES = [
    # Vorbereitung
    "vorbereitung des jahresabschlusses",
    "vorbereitung von jahresabschlüssen",
    "vorbereitung der jahresabschlüsse",
    "vorbereitung der monats-",
    "vorbereitung von monats-",
    # Unterstützung
    "unterstützung bei jahresabschlüssen",
    "unterstuetzung bei jahresabschlüssen",
    "unterstützung bei der erstellung",
    "unterstuetzung bei der erstellung",
    "unterstützung bei monats-",
    "unterstuetzung bei monats-",
    # Mitwirkung
    "mitwirkung bei jahresabschlüssen",
    "mitwirkung an der erstellung",
    "mitwirkung bei der erstellung",
    "mitwirkung bei monats- und jahresabschlüssen",
    "mitwirkung bei monats-",
    # Mitarbeit
    "mitarbeit bei monats- und jahresabschlüssen",
    "mitarbeit bei jahresabschlüssen",
    "mitarbeit bei monats-",
    "mitarbeit an monats-",
    "mitarbeit an jahresabschlüssen",
    # Zuarbeit
    "zuarbeit für den jahresabschluss",
    "zuarbeit fuer den jahresabschluss",
    "zuarbeit bei jahresabschlüssen",
    # Mithilfe
    "mithilfe bei jahresabschlüssen",
    "mithilfe bei monats-",
]


def validate_job_classification(gpt_result: dict, job_text: str) -> dict:
    """Deterministische Regelvalidierung nach GPT-Klassifizierung.

    Korrigiert systematische GPT-Fehler bei:
    1. JA-Erstellung OHNE Prep-Phrase = BiBu (nicht FiBu)
    2. JA-Prep-Phrase vorhanden = KEIN BiBu (auch wenn GPT BiBu sagt)
    3. Nur Kreditoren = KrediBu (nicht FiBu)
    4. Nur Debitoren = DebiBu (nicht FiBu)
    """
    if not job_text:
        return gpt_result

    text_lower = job_text.lower()
    corrections = []

    # REGEL 1: JA-Erstellung vs. JA-Vorbereitung
    has_ja_creation = any(p in text_lower for p in _JA_CREATION_PHRASES)
    has_ja_prep = any(p in text_lower for p in _JA_PREP_PHRASES)

    # WICHTIG: Wenn Prep-Phrasen vorhanden sind, hat Creation KEIN Gewicht!
    # "Mitwirkung bei Erstellung von Monats- und Jahresabschlüssen" → FiBu, NICHT BiBu
    if has_ja_prep:
        # Prep-Phrase gefunden → KEIN BiBu, egal was sonst noch im Text steht
        if gpt_result.get("primary_role") == "Bilanzbuchhalter/in":
            gpt_result["primary_role"] = "Finanzbuchhalter/in"
            gpt_result["sub_level"] = "senior"
            if "Finanzbuchhalter/in" not in gpt_result.get("roles", []):
                gpt_result.setdefault("roles", []).append("Finanzbuchhalter/in")
            corrections.append("JA-Prep erkannt (Mitarbeit/Mitwirkung/Vorbereitung) → FiBu senior, NICHT BiBu")
    elif has_ja_creation and not has_ja_prep:
        # Echte JA-Erstellung OHNE Prep → BiBu
        if gpt_result.get("primary_role") != "Bilanzbuchhalter/in":
            gpt_result["primary_role"] = "Bilanzbuchhalter/in"
            if "Bilanzbuchhalter/in" not in gpt_result.get("roles", []):
                gpt_result.setdefault("roles", []).append("Bilanzbuchhalter/in")
            corrections.append("Eigenstaendige JA-Erstellung erkannt → BiBu")

    # REGEL 2: Nur Kreditoren (ohne Debitoren) = KrediBu
    has_kredi = "kreditorenbuchhaltung" in text_lower or "accounts payable" in text_lower
    has_debi = "debitorenbuchhaltung" in text_lower or "accounts receivable" in text_lower
    # Pruefe ob BEIDES vorkommt (dann ist es FiBu, nicht KrediBu/DebiBu)
    has_both = has_kredi and has_debi

    if has_kredi and not has_debi and not has_both:
        if gpt_result.get("primary_role") == "Finanzbuchhalter/in":
            # Nur korrigieren wenn der Job wirklich NUR Kreditoren beschreibt
            # und nicht auch "laufende Buchhaltung" o.ae.
            laufende_bu = any(p in text_lower for p in [
                "laufende buchhaltung", "laufende finanzbuchhaltung",
                "finanzbuchhaltung", "general ledger", "hauptbuchhaltung"
            ])
            if not laufende_bu:
                gpt_result["primary_role"] = "Kreditorenbuchhalter/in"
                if "Kreditorenbuchhalter/in" not in gpt_result.get("roles", []):
                    gpt_result.setdefault("roles", []).append("Kreditorenbuchhalter/in")
                corrections.append("Nur Kreditoren → KrediBu")

    # REGEL 3: Nur Debitoren (ohne Kreditoren) = DebiBu
    if has_debi and not has_kredi and not has_both:
        if gpt_result.get("primary_role") == "Finanzbuchhalter/in":
            laufende_bu = any(p in text_lower for p in [
                "laufende buchhaltung", "laufende finanzbuchhaltung",
                "finanzbuchhaltung", "general ledger", "hauptbuchhaltung"
            ])
            if not laufende_bu:
                gpt_result["primary_role"] = "Debitorenbuchhalter/in"
                if "Debitorenbuchhalter/in" not in gpt_result.get("roles", []):
                    gpt_result.setdefault("roles", []).append("Debitorenbuchhalter/in")
                corrections.append("Nur Debitoren → DebiBu")

    if corrections:
        existing_reasoning = gpt_result.get("reasoning", "")
        gpt_result["reasoning"] = f"{existing_reasoning} [REGELKORREKTUR: {', '.join(corrections)}]"
        gpt_result["title_was_corrected"] = True

    return gpt_result


def validate_candidate_classification(gpt_result: dict, candidate_text: str) -> dict:
    """Deterministische Regelvalidierung nach GPT-Klassifizierung fuer Kandidaten.

    Korrigiert systematische GPT-Fehler bei:
    1. JA-Erstellung + BiBu-Qualifikation im Werdegang = BiBu
    2. JA-Prep (Mitarbeit/Mitwirkung) = KEIN BiBu, auch wenn GPT BiBu sagt
    3. Nur Kreditoren-Taetigkeit = KrediBu (nicht FiBu)
    4. Nur Debitoren-Taetigkeit = DebiBu (nicht FiBu)
    """
    if not candidate_text:
        return gpt_result

    text_lower = candidate_text.lower()
    corrections = []

    # REGEL 1: JA-Erstellung vs. JA-Vorbereitung
    has_ja_creation = any(p in text_lower for p in _JA_CREATION_PHRASES)
    has_ja_prep = any(p in text_lower for p in _JA_PREP_PHRASES)

    # Pruefe ob Kandidat BiBu-Zertifizierung hat
    has_bibu_cert = any(kw in text_lower for kw in [
        "bilanzbuchhalter ihk", "bilanzbuchhalter (ihk)",
        "geprüfter bilanzbuchhalter", "gepruefter bilanzbuchhalter",
        "bilanzbuchhalter-weiterbildung", "bilanzbuchhalter weiterbildung",
    ])

    if has_ja_prep:
        # Prep-Phrase gefunden → KEIN BiBu, egal was sonst steht
        if gpt_result.get("primary_role") == "Bilanzbuchhalter/in" and not has_bibu_cert:
            gpt_result["primary_role"] = "Finanzbuchhalter/in"
            gpt_result["sub_level"] = "senior"
            if "Finanzbuchhalter/in" not in gpt_result.get("roles", []):
                gpt_result.setdefault("roles", []).append("Finanzbuchhalter/in")
            corrections.append("JA-Prep erkannt (Mitarbeit/Mitwirkung) ohne BiBu-Zertifikat → FiBu senior")
    elif has_ja_creation and has_bibu_cert:
        # Echte JA-Erstellung + BiBu-Zertifizierung → BiBu
        if gpt_result.get("primary_role") != "Bilanzbuchhalter/in":
            gpt_result["primary_role"] = "Bilanzbuchhalter/in"
            if "Bilanzbuchhalter/in" not in gpt_result.get("roles", []):
                gpt_result.setdefault("roles", []).append("Bilanzbuchhalter/in")
            corrections.append("Eigenstaendige JA-Erstellung + BiBu-Zertifikat → BiBu")
    elif has_ja_creation and not has_bibu_cert:
        # JA-Erstellung OHNE BiBu-Zertifizierung → FiBu senior (nicht BiBu!)
        if gpt_result.get("primary_role") == "Bilanzbuchhalter/in":
            gpt_result["primary_role"] = "Finanzbuchhalter/in"
            gpt_result["sub_level"] = "senior"
            if "Finanzbuchhalter/in" not in gpt_result.get("roles", []):
                gpt_result.setdefault("roles", []).append("Finanzbuchhalter/in")
            corrections.append("JA-Erstellung aber KEIN BiBu-Zertifikat → FiBu senior")

    # REGEL 2: Nur Kreditoren (ohne Debitoren) = KrediBu
    has_kredi = any(kw in text_lower for kw in [
        "kreditorenbuchhaltung", "accounts payable", "kreditoren",
        "eingangsrechnungen", "rechnungsprüfung", "rechnungspruefung",
    ])
    has_debi = any(kw in text_lower for kw in [
        "debitorenbuchhaltung", "accounts receivable", "debitoren",
        "mahnwesen", "forderungsmanagement",
    ])
    has_both = has_kredi and has_debi
    # Breite FiBu-Taetigkeiten: Wenn der Kandidat AUCH FiBu macht, nicht korrigieren
    has_fibu_breadth = any(p in text_lower for p in [
        "laufende buchhaltung", "laufende finanzbuchhaltung",
        "finanzbuchhaltung", "hauptbuchhaltung", "sachkontenbuchhaltung",
        "monatsabschluss", "jahresabschluss", "anlagenbuchhaltung",
    ])

    if has_kredi and not has_debi and not has_both and not has_fibu_breadth:
        if gpt_result.get("primary_role") == "Finanzbuchhalter/in":
            gpt_result["primary_role"] = "Kreditorenbuchhalter/in"
            if "Kreditorenbuchhalter/in" not in gpt_result.get("roles", []):
                gpt_result.setdefault("roles", []).append("Kreditorenbuchhalter/in")
            corrections.append("Nur Kreditoren → KrediBu")

    # REGEL 3: Nur Debitoren (ohne Kreditoren) = DebiBu
    if has_debi and not has_kredi and not has_both and not has_fibu_breadth:
        if gpt_result.get("primary_role") == "Finanzbuchhalter/in":
            gpt_result["primary_role"] = "Debitorenbuchhalter/in"
            if "Debitorenbuchhalter/in" not in gpt_result.get("roles", []):
                gpt_result.setdefault("roles", []).append("Debitorenbuchhalter/in")
            corrections.append("Nur Debitoren → DebiBu")

    if corrections:
        existing_reasoning = gpt_result.get("reasoning", "")
        gpt_result["reasoning"] = f"{existing_reasoning} [REGELKORREKTUR: {', '.join(corrections)}]"
        gpt_result["title_was_corrected"] = True

    return gpt_result


# ═══════════════════════════════════════════════════════════════
# DATACLASSES
# ═══════════════════════════════════════════════════════════════

@dataclass
class ClassificationResult:
    """Ergebnis einer Finance-Rollen-Klassifizierung."""

    is_leadership: bool = False
    roles: list[str] = field(default_factory=list)
    primary_role: str | None = None
    reasoning: str = ""
    success: bool = True
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    # V2-Felder fuer Deep Classification
    sub_level: str | None = None  # "normal" / "senior" (nur bei FiBu)
    quality_score: str | None = None  # "high" / "medium" / "low"
    quality_reason: str | None = None  # Begruendung fuer quality_score
    original_title: str | None = None  # Original-Titel aus CSV
    corrected_title: str | None = None  # Korrigierter Titel
    title_was_corrected: bool = False  # Titel wurde geaendert?
    job_tasks: str | None = None  # Extrahierte Taetigkeiten (kommasepariert)

    @property
    def cost_usd(self) -> float:
        input_cost = (self.input_tokens / 1_000_000) * PRICE_INPUT_PER_1M
        output_cost = (self.output_tokens / 1_000_000) * PRICE_OUTPUT_PER_1M
        return round(input_cost + output_cost, 6)


@dataclass
class BatchClassificationResult:
    """Ergebnis einer Batch-Klassifizierung."""

    total: int = 0
    classified: int = 0
    skipped_leadership: int = 0
    skipped_no_cv: int = 0
    skipped_no_role: int = 0
    skipped_error: int = 0
    multi_title_count: int = 0
    roles_distribution: dict[str, int] = field(default_factory=dict)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    duration_seconds: float = 0.0
    # Listen für Analyse — ALLE Kandidaten nach Kategorie
    classified_candidates: list[dict] = field(default_factory=list)
    unclassified_candidates: list[dict] = field(default_factory=list)
    leadership_candidates: list[dict] = field(default_factory=list)
    error_candidates: list[dict] = field(default_factory=list)

    @property
    def cost_usd(self) -> float:
        input_cost = (self.total_input_tokens / 1_000_000) * PRICE_INPUT_PER_1M
        output_cost = (self.total_output_tokens / 1_000_000) * PRICE_OUTPUT_PER_1M
        return round(input_cost + output_cost, 4)


# ═══════════════════════════════════════════════════════════════
# SERVICE
# ═══════════════════════════════════════════════════════════════

class FinanceClassifierService:
    """Klassifiziert FINANCE-Kandidaten/Jobs via OpenAI anhand des Werdegangs."""

    MODEL = "gpt-4o"

    def __init__(self, db: AsyncSession, api_key: str | None = None):
        self.db = db
        self.api_key = api_key or settings.openai_api_key
        self._client: httpx.AsyncClient | None = None
        self._last_error: str | None = None  # Letzter Fehler fuer Debugging

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url="https://api.openai.com/v1",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=httpx.Timeout(limits.TIMEOUT_OPENAI),
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    # ──────────────────────────────────────────────────
    # OpenAI API Call
    # ──────────────────────────────────────────────────

    async def _call_openai(
        self,
        system_prompt: str,
        user_prompt: str,
        retry_count: int = 5,
    ) -> dict[str, Any] | None:
        """Sendet einen Prompt an OpenAI und gibt die JSON-Antwort zurück.

        Bei 429 Rate-Limit: Wartet retry-after Header oder 30/60/90/120/150s
        mit zufaelligem Jitter (±5s) damit parallele Tasks nicht gleichzeitig retrien.
        """
        import asyncio
        import random

        if not self.api_key:
            logger.warning("OpenAI API-Key nicht konfiguriert")
            return None

        for attempt in range(retry_count + 1):
            try:
                client = await self._get_client()
                response = await client.post(
                    "/chat/completions",
                    json={
                        "model": self.MODEL,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                        "temperature": 0.1,
                        "max_tokens": 500,
                        "response_format": {"type": "json_object"},
                    },
                )
                response.raise_for_status()
                result = response.json()

                # Usage extrahieren
                usage = result.get("usage", {})
                content = result["choices"][0]["message"]["content"]
                parsed = json.loads(content)
                parsed["_usage"] = {
                    "input_tokens": usage.get("prompt_tokens", 0),
                    "output_tokens": usage.get("completion_tokens", 0),
                }
                return parsed

            except httpx.TimeoutException:
                if attempt < retry_count:
                    logger.warning(
                        f"Finance-Classifier Timeout, Versuch {attempt + 2}/{retry_count + 1}"
                    )
                    await asyncio.sleep(5 * (attempt + 1))
                    continue
                self._last_error = "Timeout nach allen Versuchen"
                logger.error(f"Finance-Classifier {self._last_error}")
                return None

            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:
                    # Rate-Limit: Warte mit exponential Backoff + Jitter
                    if attempt < retry_count:
                        retry_after = e.response.headers.get("retry-after")
                        if retry_after:
                            wait = int(retry_after) + random.uniform(1, 5)
                        else:
                            wait = 30 * (attempt + 1) + random.uniform(1, 10)
                        logger.warning(
                            f"OpenAI 429 Rate-Limit, warte {wait:.0f}s "
                            f"(Versuch {attempt + 2}/{retry_count + 1})"
                        )
                        await asyncio.sleep(wait)
                        continue
                    self._last_error = "429 Rate-Limit nach allen Retries"
                    logger.error(f"Finance-Classifier {self._last_error}")
                    return None
                self._last_error = f"HTTPStatusError: {str(e)[:200]}"
                logger.error(f"Finance-Classifier Fehler: {self._last_error}")
                return None

            except (json.JSONDecodeError, KeyError) as e:
                self._last_error = f"{type(e).__name__}: {str(e)[:200]}"
                logger.error(f"Finance-Classifier Fehler: {self._last_error}")
                return None

            except Exception as e:
                self._last_error = f"{type(e).__name__}: {str(e)[:200]}"
                logger.error(f"Finance-Classifier unerwarteter Fehler: {self._last_error}")
                return None

        return None

    # ──────────────────────────────────────────────────
    # Kandidat klassifizieren
    # ──────────────────────────────────────────────────

    def _build_candidate_prompt(self, candidate: Candidate) -> str:
        """Baut den User-Prompt für einen Kandidaten."""
        parts = []

        parts.append(f"AKTUELLE POSITION: {candidate.current_position or 'Unbekannt'}")

        # Work History
        if candidate.work_history:
            parts.append("\nWERDEGANG:")
            entries = candidate.work_history if isinstance(candidate.work_history, list) else []
            for i, entry in enumerate(entries, 1):
                if isinstance(entry, dict):
                    pos = entry.get("position", "Unbekannt")
                    company = entry.get("company", "Unbekannt")
                    start = entry.get("start_date", "?")
                    end = entry.get("end_date", "?")
                    desc = entry.get("description", "")
                    parts.append(f"\n{i}. {pos} bei {company} ({start} - {end})")
                    if desc:
                        parts.append(f"   Tätigkeiten: {desc}")

        # Education
        if candidate.education:
            parts.append("\nAUSBILDUNG:")
            entries = candidate.education if isinstance(candidate.education, list) else []
            for entry in entries:
                if isinstance(entry, dict):
                    degree = entry.get("degree", "")
                    institution = entry.get("institution", "")
                    field = entry.get("field_of_study", "")
                    parts.append(f"- {degree} ({field}) — {institution}")

        # Further Education (Bilanzbuchhalter IHK etc.)
        if candidate.further_education:
            parts.append("\nWEITERBILDUNGEN / ZERTIFIKATE:")
            entries = candidate.further_education if isinstance(candidate.further_education, list) else []
            for entry in entries:
                if isinstance(entry, dict):
                    degree = entry.get("degree", "")
                    institution = entry.get("institution", "")
                    parts.append(f"- {degree} — {institution}")

        # IT Skills
        if candidate.it_skills:
            parts.append(f"\nIT-KENNTNISSE: {', '.join(candidate.it_skills)}")

        return "\n".join(parts)

    async def classify_candidate(self, candidate: Candidate) -> ClassificationResult:
        """Klassifiziert einen einzelnen FINANCE-Kandidaten via OpenAI."""

        # Kein Werdegang → überspringen
        if not candidate.work_history and not candidate.current_position:
            return ClassificationResult(
                success=False,
                error="Kein Werdegang vorhanden",
                reasoning="Kein Werdegang vorhanden",
            )

        user_prompt = self._build_candidate_prompt(candidate)
        result = await self._call_openai(FINANCE_CLASSIFIER_SYSTEM_PROMPT, user_prompt)

        if result is None:
            return ClassificationResult(
                success=False,
                error=f"OpenAI: {self._last_error or 'unbekannt'}",
            )

        # Usage extrahieren
        usage = result.pop("_usage", {})
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)

        # V3: Deterministische Regelvalidierung NACH GPT-Antwort
        candidate_text = user_prompt  # Vollstaendiger Werdegang-Text
        result = validate_candidate_classification(result, candidate_text)

        # Ergebnis parsen
        is_leadership = result.get("is_leadership", False)
        roles = result.get("roles", [])
        primary_role = result.get("primary_role")
        reasoning = result.get("reasoning", "")

        # Rollen validieren — nur erlaubte Werte
        roles = [r for r in roles if r in ALLOWED_ROLES]
        if primary_role and primary_role not in ALLOWED_ROLES:
            primary_role = roles[0] if roles else None

        # V2-Felder parsen (einheitlich wie bei Jobs)
        sub_level = result.get("sub_level")
        if sub_level not in ("normal", "senior"):
            sub_level = "normal"

        return ClassificationResult(
            is_leadership=is_leadership,
            roles=roles,
            primary_role=primary_role,
            reasoning=reasoning,
            success=True,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            sub_level=sub_level,
        )

    # ──────────────────────────────────────────────────
    # Job klassifizieren
    # ──────────────────────────────────────────────────

    def _build_job_prompt(self, job: Job) -> str:
        """Baut den User-Prompt für einen Job."""
        parts = []
        parts.append(f"STELLENTITEL: {job.position or 'Unbekannt'}")
        if job.company_name:
            parts.append(f"UNTERNEHMEN: {job.company_name}")
        if job.job_text:
            parts.append(f"\nSTELLENBESCHREIBUNG:\n{job.job_text[:8000]}")
        return "\n".join(parts)

    async def classify_job(self, job: Job) -> ClassificationResult:
        """Klassifiziert einen einzelnen FINANCE-Job via OpenAI (V2 mit Quality Gate)."""
        if not job.job_text and not job.position:
            return ClassificationResult(
                success=False,
                error="Keine Stellenbeschreibung vorhanden",
                quality_score="low",
                quality_reason="Keine Stellenbeschreibung vorhanden",
            )

        user_prompt = self._build_job_prompt(job)
        result = await self._call_openai(FINANCE_JOB_CLASSIFIER_PROMPT, user_prompt)

        if result is None:
            return ClassificationResult(success=False, error=f"OpenAI: {self._last_error or 'unbekannt'}")

        usage = result.pop("_usage", {})

        # V3: Deterministische Regelvalidierung NACH GPT-Antwort
        job_text_for_validation = job.job_text or job.position or ""
        result = validate_job_classification(result, job_text_for_validation)

        roles = [r for r in result.get("roles", []) if r in ALLOWED_ROLES]
        primary_role = result.get("primary_role")
        if primary_role and primary_role not in ALLOWED_ROLES:
            primary_role = roles[0] if roles else None

        # V2-Felder parsen
        sub_level = result.get("sub_level")
        if sub_level not in ("normal", "senior"):
            sub_level = "normal"

        quality_score = result.get("quality_score")
        if quality_score not in ("high", "medium", "low"):
            quality_score = "medium"  # Fallback

        return ClassificationResult(
            is_leadership=result.get("is_leadership", False),
            roles=roles,
            primary_role=primary_role,
            reasoning=result.get("reasoning", ""),
            success=True,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            sub_level=sub_level,
            quality_score=quality_score,
            quality_reason=result.get("quality_reason", ""),
            original_title=result.get("original_title", job.position),
            corrected_title=result.get("corrected_title"),
            title_was_corrected=result.get("title_was_corrected", False),
            job_tasks=result.get("job_tasks"),
        )

    # ──────────────────────────────────────────────────
    # Ergebnis auf Kandidat/Job anwenden
    # ──────────────────────────────────────────────────

    def apply_to_candidate(self, candidate: Candidate, result: ClassificationResult) -> None:
        """Setzt die Klassifizierungsergebnisse auf dem Kandidaten-Model (V2 — einheitlich mit Jobs)."""
        if result.roles:
            candidate.hotlist_job_title = result.primary_role or result.roles[0]
            candidate.hotlist_job_titles = result.roles
        # V2: classification_data einheitlich wie bei Jobs speichern
        candidate.classification_data = {
            "source": "openai_v2",
            "is_leadership": result.is_leadership,
            "roles": result.roles,
            "primary_role": result.primary_role,
            "sub_level": result.sub_level,
            "reasoning": result.reasoning,
            "classified_at": datetime.now(timezone.utc).isoformat(),
        }

    def apply_to_job(self, job: Job, result: ClassificationResult) -> None:
        """Setzt die Klassifizierungsergebnisse auf dem Job-Model (V2 mit Deep Classification)."""
        if result.roles:
            job.hotlist_job_title = result.primary_role or result.roles[0]
            job.hotlist_job_titles = result.roles

        # V2: classification_data + quality_score speichern
        job.classification_data = {
            "source": "openai_v2",
            "is_leadership": result.is_leadership,
            "roles": result.roles,
            "primary_role": result.primary_role,
            "sub_level": result.sub_level,
            "reasoning": result.reasoning,
            "quality_score": result.quality_score,
            "quality_reason": result.quality_reason,
            "original_title": result.original_title or job.position,
            "corrected_title": result.corrected_title,
            "title_was_corrected": result.title_was_corrected,
            "classified_at": datetime.now(timezone.utc).isoformat(),
        }
        job.quality_score = result.quality_score
        if result.job_tasks:
            job.job_tasks = result.job_tasks[:500]  # Max 500 Zeichen

    # ──────────────────────────────────────────────────
    # Batch-Klassifizierung: Alle FINANCE-Kandidaten
    # ──────────────────────────────────────────────────

    async def classify_all_finance_candidates(
        self, force: bool = False, progress_callback=None,
    ) -> BatchClassificationResult:
        """Klassifiziert alle FINANCE-Kandidaten via OpenAI (parallel, 5 gleichzeitig)."""
        import asyncio
        start_time = datetime.now(timezone.utc)

        # Alle FINANCE-Kandidaten laden
        query = (
            select(Candidate)
            .where(
                and_(
                    Candidate.hotlist_category == "FINANCE",
                    Candidate.deleted_at.is_(None),
                )
            )
        )
        if not force:
            # Nur Kandidaten ohne classification_data
            query = query.where(Candidate.classification_data.is_(None))

        result = await self.db.execute(query)
        candidates = list(result.scalars().all())

        batch_result = BatchClassificationResult(total=len(candidates))
        logger.info(f"Finance-Klassifizierung: {len(candidates)} Kandidaten zu verarbeiten (parallel, 5 gleichzeitig)")

        # Semaphore fuer max 5 parallele OpenAI-Requests
        semaphore = asyncio.Semaphore(5)
        processed_count = 0

        async def _classify_one(candidate: Candidate) -> None:
            """Klassifiziert einen Kandidaten mit Semaphore-Begrenzung."""
            nonlocal processed_count
            async with semaphore:
                try:
                    classification = await self.classify_candidate(candidate)

                    batch_result.total_input_tokens += classification.input_tokens
                    batch_result.total_output_tokens += classification.output_tokens

                    if not classification.success:
                        if classification.error == "Kein Werdegang vorhanden":
                            batch_result.skipped_no_cv += 1
                        else:
                            batch_result.skipped_error += 1
                            batch_result.error_candidates.append({
                                "id": str(candidate.id),
                                "name": candidate.full_name,
                                "position": candidate.current_position,
                                "error": classification.error,
                            })
                        return

                    if classification.is_leadership:
                        batch_result.skipped_leadership += 1
                        batch_result.leadership_candidates.append({
                            "id": str(candidate.id),
                            "name": candidate.full_name,
                            "position": candidate.current_position,
                            "reasoning": classification.reasoning,
                        })
                        self.apply_to_candidate(candidate, classification)
                        # Leadership-Kandidaten werden jetzt TROTZDEM klassifiziert
                        # (Prompt gibt roles + primary_role zurueck, auch bei is_leadership=true)
                        if classification.roles:
                            batch_result.classified += 1
                            for role in classification.roles:
                                batch_result.roles_distribution[role] = (
                                    batch_result.roles_distribution.get(role, 0) + 1
                                )
                        return

                    if not classification.roles:
                        batch_result.skipped_no_role += 1
                        batch_result.unclassified_candidates.append({
                            "id": str(candidate.id),
                            "name": candidate.full_name,
                            "position": candidate.current_position,
                            "reasoning": classification.reasoning,
                        })
                        self.apply_to_candidate(candidate, classification)
                        return

                    # Ergebnis anwenden
                    self.apply_to_candidate(candidate, classification)
                    batch_result.classified += 1
                    batch_result.classified_candidates.append({
                        "id": str(candidate.id),
                        "name": candidate.full_name,
                        "position": candidate.current_position,
                        "roles": classification.roles,
                        "primary_role": classification.primary_role,
                        "sub_level": classification.sub_level,
                        "reasoning": classification.reasoning,
                    })

                    if len(classification.roles) > 1:
                        batch_result.multi_title_count += 1

                    for role in classification.roles:
                        batch_result.roles_distribution[role] = (
                            batch_result.roles_distribution.get(role, 0) + 1
                        )

                except Exception as e:
                    logger.error(f"Fehler bei Kandidat {candidate.id}: {e}")
                    batch_result.skipped_error += 1

                finally:
                    processed_count += 1

        # In Chunks von 50 verarbeiten fuer regelmaessiges Commit + Logging
        chunk_size = 50
        for chunk_start in range(0, len(candidates), chunk_size):
            chunk = candidates[chunk_start:chunk_start + chunk_size]

            # Alle Kandidaten im Chunk parallel starten (Semaphore begrenzt auf 5)
            tasks = [_classify_one(c) for c in chunk]
            await asyncio.gather(*tasks)

            # Chunk committen
            await self.db.commit()

            # Fortschritt loggen
            done = min(chunk_start + chunk_size, len(candidates))
            elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
            rate = done / elapsed if elapsed > 0 else 0
            eta = (len(candidates) - done) / rate if rate > 0 else 0
            logger.info(
                f"Finance-Klassifizierung: {done}/{len(candidates)} "
                f"({batch_result.classified} klassifiziert, "
                f"${batch_result.cost_usd:.2f}, "
                f"{rate:.1f}/s, ETA {eta:.0f}s)"
            )
            if progress_callback:
                progress_callback(done, len(candidates), batch_result)

        # Finale Commit
        await self.db.commit()
        await self.close()

        duration = (datetime.now(timezone.utc) - start_time).total_seconds()
        batch_result.duration_seconds = round(duration, 1)

        logger.info(
            f"Finance-Klassifizierung abgeschlossen: "
            f"{batch_result.classified}/{batch_result.total} klassifiziert, "
            f"{batch_result.skipped_leadership} Fuehrungskraefte, "
            f"{batch_result.multi_title_count} Multi-Titel, "
            f"${batch_result.cost_usd:.2f} in {batch_result.duration_seconds}s"
        )

        return batch_result

    # ──────────────────────────────────────────────────
    # Batch-Klassifizierung: Alle FINANCE-Jobs
    # ──────────────────────────────────────────────────

    async def classify_all_finance_jobs(
        self, force: bool = False
    ) -> BatchClassificationResult:
        """Klassifiziert alle FINANCE-Jobs via OpenAI."""
        import asyncio
        start_time = datetime.now(timezone.utc)

        query = (
            select(Job)
            .where(
                and_(
                    Job.hotlist_category == "FINANCE",
                    Job.deleted_at.is_(None),
                )
            )
        )
        if not force:
            query = query.where(Job.hotlist_job_titles.is_(None))

        result = await self.db.execute(query)
        jobs = list(result.scalars().all())

        batch_result = BatchClassificationResult(total=len(jobs))
        logger.info(f"Finance-Job-Klassifizierung: {len(jobs)} Jobs zu verarbeiten")

        for i, job in enumerate(jobs):
            try:
                classification = await self.classify_job(job)

                batch_result.total_input_tokens += classification.input_tokens
                batch_result.total_output_tokens += classification.output_tokens

                if not classification.success:
                    batch_result.skipped_error += 1
                    continue

                if not classification.roles:
                    batch_result.skipped_no_role += 1
                    continue

                self.apply_to_job(job, classification)
                batch_result.classified += 1

                if len(classification.roles) > 1:
                    batch_result.multi_title_count += 1

                for role in classification.roles:
                    batch_result.roles_distribution[role] = (
                        batch_result.roles_distribution.get(role, 0) + 1
                    )

                if (i + 1) % 50 == 0:
                    logger.info(f"Finance-Job-Klassifizierung: {i + 1}/{len(jobs)}")

                if (i + 1) % 10 == 0:
                    await asyncio.sleep(0.5)

            except Exception as e:
                logger.error(f"Fehler bei Job {job.id}: {e}")
                batch_result.skipped_error += 1

        await self.db.commit()
        await self.close()

        duration = (datetime.now(timezone.utc) - start_time).total_seconds()
        batch_result.duration_seconds = round(duration, 1)

        logger.info(
            f"Finance-Job-Klassifizierung abgeschlossen: "
            f"{batch_result.classified}/{batch_result.total}, "
            f"${batch_result.cost_usd:.2f} in {batch_result.duration_seconds}s"
        )

        return batch_result

    # ──────────────────────────────────────────────────
    # Deep Classification: Pipeline Step 1.5
    # ──────────────────────────────────────────────────

    async def deep_classify_finance_jobs(
        self,
        job_ids: list | None = None,
        force: bool = False,
        progress_callback=None,
    ) -> dict:
        """Deep Classification fuer FINANCE-Jobs (Pipeline Step 1.5).

        Klassifiziert Jobs mit dem V2-Prompt und speichert classification_data + quality_score.
        Wird nach der Kategorisierung (Step 1) und vor dem Geocoding (Step 2) aufgerufen.

        Args:
            job_ids: Optional — nur bestimmte Jobs klassifizieren. Wenn None, alle FINANCE-Jobs.
            force: Bereits klassifizierte Jobs nochmal klassifizieren?
            progress_callback: Callback(processed, total) fuer Fortschritts-Updates
        """
        import asyncio
        start_time = datetime.now(timezone.utc)

        # Query bauen
        query = (
            select(Job)
            .where(
                and_(
                    Job.hotlist_category == "FINANCE",
                    Job.deleted_at.is_(None),
                )
            )
        )
        if job_ids:
            query = query.where(Job.id.in_(job_ids))
        if not force:
            query = query.where(Job.classification_data.is_(None))

        result = await self.db.execute(query)
        jobs = list(result.scalars().all())

        stats = {
            "total": len(jobs),
            "classified": 0,
            "high_quality": 0,
            "medium_quality": 0,
            "low_quality": 0,
            "titles_corrected": 0,
            "leadership": 0,
            "errors": 0,
            "skipped_no_text": 0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "cost_usd": 0.0,
            "duration_seconds": 0.0,
        }

        logger.info(f"Deep Classification: {len(jobs)} FINANCE-Jobs zu verarbeiten")

        for i, job in enumerate(jobs):
            try:
                classification = await self.classify_job(job)

                stats["total_input_tokens"] += classification.input_tokens
                stats["total_output_tokens"] += classification.output_tokens

                if not classification.success:
                    stats["errors"] += 1
                    if classification.error == "Keine Stellenbeschreibung vorhanden":
                        stats["skipped_no_text"] += 1
                    continue

                # Ergebnis auf Job anwenden
                self.apply_to_job(job, classification)
                stats["classified"] += 1

                # Quality-Statistik
                qs = classification.quality_score
                if qs == "high":
                    stats["high_quality"] += 1
                elif qs == "medium":
                    stats["medium_quality"] += 1
                elif qs == "low":
                    stats["low_quality"] += 1

                if classification.title_was_corrected:
                    stats["titles_corrected"] += 1

                if classification.is_leadership:
                    stats["leadership"] += 1

                # Fortschritt
                if progress_callback and (i + 1) % 5 == 0:
                    progress_callback(i + 1, len(jobs))

                if (i + 1) % 50 == 0:
                    logger.info(
                        f"Deep Classification: {i + 1}/{len(jobs)} "
                        f"(H:{stats['high_quality']} M:{stats['medium_quality']} L:{stats['low_quality']})"
                    )

                # Rate-Limiting
                if (i + 1) % 10 == 0:
                    await asyncio.sleep(0.5)

                # Zwischenspeichern
                if (i + 1) % 50 == 0:
                    await self.db.commit()

            except Exception as e:
                logger.error(f"Deep Classification Fehler bei Job {job.id}: {e}")
                stats["errors"] += 1

        await self.db.commit()
        await self.close()

        duration = (datetime.now(timezone.utc) - start_time).total_seconds()
        stats["duration_seconds"] = round(duration, 1)
        input_cost = (stats["total_input_tokens"] / 1_000_000) * PRICE_INPUT_PER_1M
        output_cost = (stats["total_output_tokens"] / 1_000_000) * PRICE_OUTPUT_PER_1M
        stats["cost_usd"] = round(input_cost + output_cost, 4)

        logger.info(
            f"Deep Classification abgeschlossen: {stats['classified']}/{stats['total']} Jobs, "
            f"Quality H:{stats['high_quality']} M:{stats['medium_quality']} L:{stats['low_quality']}, "
            f"{stats['titles_corrected']} Titel korrigiert, "
            f"${stats['cost_usd']:.2f} in {stats['duration_seconds']}s"
        )

        return stats
