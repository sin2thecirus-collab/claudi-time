"""
Service für die Generierung von Sincirus Branded Kandidaten-Profil-PDFs.

Lädt Kandidaten-Daten aus der DB, bereitet sie auf und generiert
über WeasyPrint ein professionelles A4-PDF im Sincirus Dark Design.
"""

import asyncio
import logging
import os
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from jinja2 import Environment, FileSystemLoader
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Sprach-Level → Prozent-Balkenbreite Mapping
# ──────────────────────────────────────────────
LANG_LEVEL_MAP = {
    "muttersprache": (100, "Muttersprache"),
    "muttersprachlich": (100, "Muttersprache"),
    "native": (100, "Muttersprache"),
    "fließend": (95, "Fließend"),
    "fliessend": (95, "Fließend"),
    "fluent": (95, "Fließend"),
    "c2": (95, "C2 – Fließend"),
    "verhandlungssicher": (90, "Verhandlungssicher"),
    "c1": (85, "C1 – Fortgeschritten"),
    "gut": (70, "Gut"),
    "good": (70, "Gut"),
    "b2": (70, "B2 – Gut"),
    "b1": (55, "B1 – Mittelstufe"),
    "intermediate": (55, "Mittelstufe"),
    "grundkenntnisse": (35, "Grundkenntnisse"),
    "basic": (35, "Grundkenntnisse"),
    "a2": (35, "A2 – Grundkenntnisse"),
    "a1": (20, "A1 – Anfänger"),
    "beginner": (20, "Anfänger"),
}

# ──────────────────────────────────────────────
# ERP Proficiency → Prozent Mapping
# ──────────────────────────────────────────────
PROFICIENCY_MAP = {
    "experte": (90, "Experte"),
    "expert": (90, "Experte"),
    "fortgeschritten": (70, "Fortgeschritten"),
    "advanced": (70, "Fortgeschritten"),
    "grundlagen": (35, "Grundkenntnisse"),
    "grundkenntnisse": (35, "Grundkenntnisse"),
    "basic": (35, "Grundkenntnisse"),
}

# Bekannte ERP-Systeme (für Skill-Tags als highlight markieren)
KNOWN_ERP = {
    "sap", "datev", "addison", "lexware", "sage", "navision",
    "dynamics", "oracle", "datis", "diamant", "varial",
    "loga", "p&i loga", "paisy", "lodas", "abas", "proalpha",
}


class ProfilePdfService:
    """Generiert Sincirus Branded Kandidaten-Profile als PDF."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def generate_profile_pdf(self, candidate_id: UUID) -> bytes:
        """
        Hauptmethode: Lädt Kandidat, bereitet Daten auf, rendert PDF.
        Gibt PDF als bytes zurück.
        """
        from app.services.candidate_service import CandidateService

        service = CandidateService(self.db)
        candidate = await service.get_candidate(candidate_id)

        if not candidate:
            raise ValueError(f"Kandidat {candidate_id} nicht gefunden")

        context = self._prepare_template_context(candidate)

        # Jinja2 Template rendern
        template_dir = os.path.join(os.path.dirname(__file__), "..", "templates")
        env = Environment(loader=FileSystemLoader(os.path.abspath(template_dir)))
        template = env.get_template("profile_sincirus_branded.html")
        html_string = template.render(**context)

        # WeasyPrint: HTML → PDF (CPU-bound, in Executor)
        from weasyprint import HTML

        static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
        base_url = os.path.abspath(static_dir)

        loop = asyncio.get_event_loop()
        pdf_bytes = await loop.run_in_executor(
            None,
            lambda: HTML(string=html_string, base_url=base_url).write_pdf(),
        )

        logger.info(f"PDF generiert für Kandidat {candidate_id} ({len(pdf_bytes)} bytes)")
        return pdf_bytes

    # ──────────────────────────────────────────────
    # Daten-Aufbereitung
    # ──────────────────────────────────────────────

    def _prepare_template_context(self, candidate) -> dict[str, Any]:
        """Bereitet alle Template-Variablen aus den Kandidaten-Daten auf."""

        # Statischer Base-Pfad für Assets
        static_dir = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "static")
        )

        quick_facts = self._build_quick_facts(candidate)
        kurzprofil_items = self._build_kurzprofil(candidate)
        erp_items = self._build_erp_items(candidate)
        languages = self._build_languages(candidate)
        skill_tags = self._build_skill_tags(candidate)
        work_items = self._build_work_items(candidate)

        # Fallback-Kurzprofil: Wenn nichts aus Qualifizierungsgespräch da ist,
        # baue ein Profil aus vorhandenen DB-Daten (Position, Standort, letzte Jobs)
        if not kurzprofil_items:
            kurzprofil_items = self._build_fallback_kurzprofil(candidate, work_items)

        # Fallback-Quick-Facts: Position + Stadt wenn keine Qualidaten vorhanden
        if not quick_facts:
            quick_facts = self._build_fallback_quick_facts(candidate)

        return {
            # Meta
            "today_date": datetime.now().strftime("%d. %B %Y").replace(
                "January", "Januar"
            ).replace("February", "Februar").replace("March", "März").replace(
                "May", "Mai"
            ).replace("June", "Juni").replace("July", "Juli").replace(
                "October", "Oktober"
            ).replace("December", "Dezember"),
            "candidate_ref": self._build_candidate_ref(candidate),

            # Hero
            "hero_title": self._build_hero_title(candidate),
            "hero_meta": self._build_hero_meta(candidate),

            # Seite 1 Content
            "quick_facts": quick_facts,
            "kurzprofil_items": kurzprofil_items,
            "erp_items": erp_items,
            "languages": languages,
            "skill_tags": skill_tags,

            # Seite 2+ Lebenslauf
            "work_items": work_items,
            "edu_items": self._build_edu_items(candidate),
            "cert_items": self._build_cert_items(candidate),
            "it_tags": self._build_it_tags(candidate),
            "cv_languages": languages,  # Nochmal für CV-Seite

            # Empfehlung
            "empfehlung_from": "Milad Hamdard • Senior Consultant Finance & Engineering",
            "empfehlung_text": self._build_empfehlung(candidate),

            # Consultant (vorerst hardcoded)
            "consultant": {
                "name": "Milad Hamdard",
                "title": "Senior Consultant Finance & Engineering",
                "email": "hamdard@sincirus.com",
                "phone": "0176 8000 47 41 \u00a0•\u00a0 040 238 345 320",
                "address": "Ballindamm 3, 20095 Hamburg",
            },

            # Asset-Pfade (absolute Pfade für WeasyPrint)
            "logo_path": os.path.join(static_dir, "images", "sincirus_logo.png"),
            "logo_komplett_path": os.path.join(static_dir, "images", "sincirus_logo_komplett.png"),
            "logo_white_path": self._logo_as_data_uri(os.path.join(static_dir, "images", "sincirus_logo_komplett_white.png")),
            "logo_transparent_path": self._logo_as_data_uri(os.path.join(static_dir, "images", "sincirus_logo_komplett_transparent.png")),
            "photo_path": os.path.join(static_dir, "images", "milad_foto.jpg"),
            "font_dir": os.path.join(static_dir, "fonts"),
        }

    def _logo_as_data_uri(self, path: str) -> str:
        """Konvertiert ein Logo-Bild in eine Base64 data-URI fuer WeasyPrint-Kompatibilitaet."""
        import base64
        try:
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            return f"data:image/png;base64,{b64}"
        except FileNotFoundError:
            return ""

    def _clean_val(self, val: Any) -> str:
        """Bereinigt einen Wert: None, 'None', 'null', 'n/a', '' → leerer String."""
        if val is None:
            return ""
        s = str(val).strip()
        if s.lower() in ("none", "null", "n/a", "na", "-", "—", "not available", ""):
            return ""
        return s

    def _build_candidate_ref(self, candidate) -> str:
        """Baut die Kandidaten-Referenznummer."""
        num = getattr(candidate, "candidate_number", None)
        if num:
            return f"SP-2026-{num:04d}"
        return "SP-2026-XXXX"

    def _build_hero_title(self, candidate) -> str:
        """Baut den Hero-Titel (Positions-/Rollenbezeichnung)."""
        # Priorität: desired_positions → v2_current_role_summary → current_position → Fallback
        title = self._clean_val(getattr(candidate, "desired_positions", None))
        if title:
            # Nur erste Position nehmen wenn kommagetrennt
            return title.split(",")[0].strip()

        summary = self._clean_val(getattr(candidate, "v2_current_role_summary", None))
        if summary:
            # Nur ersten Satz nehmen
            return summary.split(".")[0].strip()

        position = self._clean_val(getattr(candidate, "current_position", None))
        if position:
            return position

        return "Kandidatenprofil"

    def _build_hero_meta(self, candidate) -> str:
        """Baut die Hero-Meta-Zeile: Anstellung · Gehalt · Verfügbar ab."""
        parts = []

        # Anstellung
        emp = self._clean_val(getattr(candidate, "employment_type", None))
        hours = self._clean_val(getattr(candidate, "part_time_hours", None))
        if emp:
            if hours and "teilzeit" in emp.lower():
                parts.append(f"{emp} {hours}")
            else:
                parts.append(emp)

        # Gehalt
        salary = self._clean_val(getattr(candidate, "salary", None))
        if salary:
            parts.append(f"<strong>{salary}</strong>")

        # Verfügbar ab
        notice = self._clean_val(getattr(candidate, "notice_period", None))
        if notice:
            parts.append(f"ab {notice}")

        return " \u00a0·\u00a0 ".join(parts) if parts else ""

    def _build_quick_facts(self, candidate) -> list[dict]:
        """Baut die Quick Facts Karten — nur Karten mit vorhandenen Daten."""
        facts = []

        # Gehalt
        salary = self._clean_val(getattr(candidate, "salary", None))
        if salary:
            facts.append({"label": "Gehalt", "value": salary, "detail": ""})

        # Verfügbar ab / Kündigungsfrist
        notice = self._clean_val(getattr(candidate, "notice_period", None))
        if notice:
            facts.append({"label": "Verfügbar ab", "value": notice, "detail": ""})

        # Anstellung
        emp = self._clean_val(getattr(candidate, "employment_type", None))
        hours = self._clean_val(getattr(candidate, "part_time_hours", None))
        if emp:
            detail = hours if hours and "teilzeit" in emp.lower() else ""
            facts.append({"label": "Anstellung", "value": emp, "detail": detail})

        # Home-Office
        ho = self._clean_val(getattr(candidate, "home_office_days", None))
        if ho:
            facts.append({"label": "Home-Office", "value": ho, "detail": ""})

        # Pendel
        commute = self._clean_val(getattr(candidate, "commute_max", None))
        transport = self._clean_val(getattr(candidate, "commute_transport", None))
        if commute:
            detail = transport if transport else ""
            facts.append({"label": "Pendel", "value": commute, "detail": detail})

        # Großraumbüro
        office = self._clean_val(getattr(candidate, "open_office_ok", None))
        if office:
            display = {"ja": "Kein Problem", "nein": "Ungern", "egal": "Egal"}.get(
                office.lower(), office
            )
            facts.append({"label": "Großraumbüro", "value": display, "detail": ""})

        return facts

    def _build_kurzprofil(self, candidate) -> list[dict]:
        """Baut die Kurzprofil-Items (Wechselmotivation, Kernkompetenz)."""
        items = []

        notes = self._clean_val(getattr(candidate, "candidate_notes", None))
        if notes:
            items.append({"label": "Wechselmotivation", "text": notes})

        summary = self._clean_val(getattr(candidate, "v2_current_role_summary", None))
        if summary:
            items.append({"label": "Kernkompetenz", "text": summary})

        return items

    def _build_erp_items(self, candidate) -> list[dict]:
        """Baut die ERP-Einträge mit Prozent-Balken."""
        erp_list = getattr(candidate, "erp", None) or []
        structured = getattr(candidate, "v2_structured_skills", None) or []

        # Lookup: skill_name_lower → proficiency
        skill_lookup = {}
        for s in structured:
            if isinstance(s, dict) and s.get("category") == "software":
                skill_lookup[s.get("skill", "").lower()] = s.get("proficiency", "")

        items = []
        for erp_name in erp_list:
            name = self._clean_val(erp_name)
            if not name:
                continue
            proficiency = skill_lookup.get(name.lower(), "")
            if proficiency and proficiency.lower() in PROFICIENCY_MAP:
                pct, label = PROFICIENCY_MAP[proficiency.lower()]
            else:
                pct, label = 50, ""
            items.append({"name": name, "pct": pct, "level_label": label})

        return items

    def _map_language_level(self, level: str) -> tuple[int, str]:
        """Mappt einen Sprach-Level-String auf (Prozent, Anzeige-Label)."""
        if not level:
            return (50, "")

        level_lower = level.lower().strip()

        # Exakte Treffer
        if level_lower in LANG_LEVEL_MAP:
            return LANG_LEVEL_MAP[level_lower]

        # Keyword-Suche (gleiche Logik wie candidate_detail.html)
        if "mutter" in level_lower:
            return (100, "Muttersprache")
        if "flie" in level_lower or "fluent" in level_lower:
            return (95, "Fließend")
        if "verhandlung" in level_lower:
            return (90, "Verhandlungssicher")
        if "c2" in level_lower:
            return (95, "C2")
        if "c1" in level_lower:
            return (85, "C1")
        if "b2" in level_lower:
            return (70, "B2")
        if "gut" in level_lower or "good" in level_lower:
            return (70, "Gut")
        if "b1" in level_lower:
            return (55, "B1")
        if "grund" in level_lower or "basic" in level_lower:
            return (35, "Grundkenntnisse")
        if "a2" in level_lower:
            return (35, "A2")
        if "a1" in level_lower:
            return (20, "A1")

        # Fallback
        return (60, level)

    def _build_languages(self, candidate) -> list[dict]:
        """Baut die Sprach-Liste mit Prozent-Balken."""
        lang_data = getattr(candidate, "languages", None) or []
        items = []

        for entry in lang_data:
            if not isinstance(entry, dict):
                continue
            name = self._clean_val(entry.get("language", ""))
            level = self._clean_val(entry.get("level", ""))
            if not name:
                continue
            pct, label = self._map_language_level(level)
            items.append({"name": name, "pct": pct, "level_label": label})

        return items

    def _build_skill_tags(self, candidate) -> list[dict]:
        """Baut die Schlüsselqualifikationen-Tags."""
        tags = []
        seen = set()

        # ERP-Systeme als highlighted Tags
        erp_list = getattr(candidate, "erp", None) or []
        for erp in erp_list:
            name = self._clean_val(erp)
            if name and name.lower() not in seen:
                tags.append({"name": name, "highlight": True})
                seen.add(name.lower())

        # IT-Skills
        it_skills = getattr(candidate, "it_skills", None) or []
        for skill in it_skills:
            name = self._clean_val(skill)
            if name and name.lower() not in seen:
                is_highlight = name.lower() in KNOWN_ERP
                tags.append({"name": name, "highlight": is_highlight})
                seen.add(name.lower())

        return tags

    def _build_work_items(self, candidate) -> list[dict]:
        """Baut die Berufserfahrung-Karten."""
        work = getattr(candidate, "work_history", None) or []
        items = []

        for job in work:
            if not isinstance(job, dict):
                continue

            title = self._clean_val(job.get("position") or job.get("title", ""))
            company = self._clean_val(job.get("company", ""))
            if not title and not company:
                continue

            start = self._clean_val(job.get("start_date", ""))
            end = self._clean_val(job.get("end_date", ""))
            if not end or end.lower() in ("heute", "present", "current", "now", "bis heute"):
                end = "heute"

            date_range = f"{start} – {end}" if start else end

            # Beschreibung → Bullet-Liste
            desc = self._clean_val(job.get("description", ""))
            bullets = []
            if desc:
                for line in desc.split("\n"):
                    line = line.strip().lstrip("•-▸►→ ").strip()
                    if not line:
                        continue
                    # Lange Komma-getrennte Sätze in Einzel-Bullets splitten
                    # (nur wenn der Text > 120 Zeichen und Kommas enthält)
                    if len(line) > 120 and ", " in line:
                        parts = [p.strip() for p in line.split(", ") if p.strip()]
                        # Nur splitten wenn wir mind. 3 sinnvolle Teile bekommen
                        if len(parts) >= 3:
                            bullets.extend(parts)
                        else:
                            bullets.append(line)
                    else:
                        bullets.append(line)

            # Dauer
            duration = self._clean_val(job.get("duration", ""))
            note = ""
            if duration:
                note = duration

            items.append({
                "title": title or "Position",
                "company": company,
                "date_range": date_range,
                "note": note,
                "bullets": bullets,
            })

        return items

    def _build_edu_items(self, candidate) -> list[dict]:
        """Baut die Ausbildungs-Karten."""
        edu = getattr(candidate, "education", None) or []
        items = []

        for entry in edu:
            if not isinstance(entry, dict):
                continue

            degree = self._clean_val(
                entry.get("degree") or entry.get("qualification", "")
            )
            institution = self._clean_val(
                entry.get("institution") or entry.get("school", "")
            )
            if not degree and not institution:
                continue

            # Datum
            start = self._clean_val(entry.get("start_date", ""))
            end = self._clean_val(entry.get("end_date", ""))
            year = self._clean_val(entry.get("year", ""))

            if start and end:
                date_display = f"{start} – {end}"
            elif year:
                date_display = year
            elif end:
                date_display = end
            else:
                date_display = ""

            items.append({
                "name": degree or "Ausbildung",
                "institution": institution,
                "date": date_display,
            })

        return items

    def _build_cert_items(self, candidate) -> list[dict]:
        """Baut die Zertifikats-/Weiterbildungs-Karten."""
        certs = getattr(candidate, "further_education", None) or []
        items = []

        for entry in certs:
            if not isinstance(entry, dict):
                continue

            name = self._clean_val(
                entry.get("degree") or entry.get("qualification") or entry.get("name", "")
            )
            institution = self._clean_val(entry.get("institution", ""))

            if not name:
                continue

            # Generische Einträge überspringen
            if name.lower() in ("seminar", "kurs", "weiterbildung", "schulung"):
                if not institution:
                    continue

            year = self._clean_val(
                entry.get("year") or entry.get("end_date") or entry.get("start_date", "")
            )

            items.append({
                "name": name,
                "institution": institution,
                "year": year,
            })

        return items

    def _build_it_tags(self, candidate) -> list[dict]:
        """Baut die IT-Kenntnisse Tags für die CV-Seite."""
        it_skills = getattr(candidate, "it_skills", None) or []
        erp_list = set((e or "").lower() for e in (getattr(candidate, "erp", None) or []))

        tags = []
        seen = set()

        for skill in it_skills:
            name = self._clean_val(skill)
            if name and name.lower() not in seen:
                is_core = name.lower() in erp_list or name.lower() in KNOWN_ERP
                tags.append({"name": name, "core": is_core})
                seen.add(name.lower())

        return tags

    def _build_empfehlung(self, candidate) -> str:
        """Baut den Empfehlungstext. Kann später durch GPT-generierte Texte ersetzt werden."""
        summary = self._clean_val(getattr(candidate, "v2_current_role_summary", None))
        if summary:
            return summary
        return ""

    def _build_fallback_kurzprofil(self, candidate, work_items: list) -> list[dict]:
        """
        Fallback wenn kein Qualifizierungsgespräch stattgefunden hat.
        Baut ein Kurzprofil aus vorhandenen DB-Daten.
        """
        items = []

        # Aktuelle Position + Firma
        position = self._clean_val(getattr(candidate, "current_position", None))
        company = self._clean_val(getattr(candidate, "current_company", None))
        if position:
            text = position
            if company:
                text += f" bei {company}"
            items.append({"label": "Aktuelle Position", "text": text})

        # Standort
        city = self._clean_val(getattr(candidate, "city", None))
        if city:
            items.append({"label": "Standort", "text": city})

        # Berufserfahrung Zusammenfassung aus work_items
        if work_items and len(work_items) >= 2:
            years = self._clean_val(getattr(candidate, "v2_years_experience", None))
            if years:
                items.append({
                    "label": "Erfahrung",
                    "text": f"Ca. {years} Jahre Berufserfahrung, zuletzt als {work_items[0]['title']}"
                })

        # v2_seniority_level
        seniority = self._clean_val(getattr(candidate, "v2_seniority_level", None))
        if seniority:
            items.append({"label": "Senioritätslevel", "text": seniority})

        # v2_career_trajectory
        trajectory = self._clean_val(getattr(candidate, "v2_career_trajectory", None))
        if trajectory:
            items.append({"label": "Karriereverlauf", "text": trajectory})

        return items

    def _build_fallback_quick_facts(self, candidate) -> list[dict]:
        """
        Fallback Quick Facts aus Basisdaten wenn keine Qualifizierungsdaten vorhanden.
        """
        facts = []

        # Position
        position = self._clean_val(getattr(candidate, "current_position", None))
        if position:
            facts.append({"label": "Position", "value": position, "detail": ""})

        # Standort
        city = self._clean_val(getattr(candidate, "city", None))
        if city:
            facts.append({"label": "Standort", "value": city, "detail": ""})

        # Erfahrung
        years = self._clean_val(getattr(candidate, "v2_years_experience", None))
        if years:
            facts.append({"label": "Erfahrung", "value": f"{years} Jahre", "detail": ""})

        # Seniority
        seniority = self._clean_val(getattr(candidate, "v2_seniority_level", None))
        if seniority:
            facts.append({"label": "Level", "value": seniority, "detail": ""})

        return facts
