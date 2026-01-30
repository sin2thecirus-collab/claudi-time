"""Tests für das Hotlisten & DeepMatch System.

Testet:
- Categorization Service (Keywords, PLZ-Mapping, Normalisierung)
- Pre-Scoring Service (Score-Berechnung)
- Integration (CRM-Sync / CSV-Import Hooks)
"""

import pytest
from unittest.mock import MagicMock, AsyncMock
from datetime import datetime, timezone

from app.services.categorization_service import (
    CategorizationService,
    HotlistCategory,
    PLZ_CITY_MAP,
)
from app.services.pre_scoring_service import (
    PreScoringService,
    PreScoreBreakdown,
    WEIGHT_CATEGORY,
    WEIGHT_CITY,
    WEIGHT_JOB_TITLE,
    WEIGHT_KEYWORDS,
    WEIGHT_DISTANCE,
)


# ═══════════════════════════════════════════════════════════════
# CATEGORIZATION SERVICE TESTS
# ═══════════════════════════════════════════════════════════════

class TestCategorizationDetect:
    """Tests für die Kategorie-Erkennung."""

    def setup_method(self):
        self.service = CategorizationService(db=MagicMock())

    # --- FINANCE ---

    def test_detect_finance_buchhalter(self):
        category, keywords = self.service.detect_category("Buchhalter in Vollzeit")
        assert category == HotlistCategory.FINANCE
        assert "buchhalter" in keywords

    def test_detect_finance_controller(self):
        category, keywords = self.service.detect_category("Senior Controller (m/w/d)")
        assert category == HotlistCategory.FINANCE
        assert "controller" in keywords

    def test_detect_finance_steuerfachangestellte(self):
        category, keywords = self.service.detect_category("Steuerfachangestellte gesucht")
        assert category == HotlistCategory.FINANCE
        assert "steuerfachangestellte" in keywords

    def test_detect_finance_bilanzbuchhalter(self):
        category, keywords = self.service.detect_category("Bilanzbuchhalterin (IHK)")
        assert category == HotlistCategory.FINANCE
        assert "bilanzbuchhalterin" in keywords

    def test_detect_finance_datev(self):
        category, keywords = self.service.detect_category("Erfahrung mit DATEV und Buchhaltung")
        assert category == HotlistCategory.FINANCE
        assert "datev" in keywords
        assert "buchhaltung" in keywords

    # --- ENGINEERING ---

    def test_detect_engineering_servicetechniker(self):
        category, keywords = self.service.detect_category("Servicetechniker Kältetechnik")
        assert category == HotlistCategory.ENGINEERING
        assert "servicetechniker" in keywords

    def test_detect_engineering_elektriker(self):
        category, keywords = self.service.detect_category("Elektriker für Gebäudetechnik")
        assert category == HotlistCategory.ENGINEERING
        assert "elektriker" in keywords

    def test_detect_engineering_shk(self):
        category, keywords = self.service.detect_category("Anlagenmechaniker SHK (m/w/d)")
        assert category == HotlistCategory.ENGINEERING
        assert "anlagenmechaniker" in keywords

    def test_detect_engineering_mechatroniker(self):
        category, keywords = self.service.detect_category("Mechatroniker Instandhaltung")
        assert category == HotlistCategory.ENGINEERING
        assert "mechatroniker" in keywords

    def test_detect_engineering_heizungsbauer(self):
        category, keywords = self.service.detect_category("Heizungsbauer für Wärmepumpen")
        assert category == HotlistCategory.ENGINEERING
        assert "heizungsbauer" in keywords

    # --- SONSTIGE ---

    def test_detect_sonstige_marketing(self):
        category, keywords = self.service.detect_category("Marketing Manager")
        assert category == HotlistCategory.SONSTIGE
        assert keywords == []

    def test_detect_sonstige_empty(self):
        category, keywords = self.service.detect_category("")
        assert category == HotlistCategory.SONSTIGE
        assert keywords == []

    def test_detect_sonstige_none(self):
        category, keywords = self.service.detect_category(None)
        assert category == HotlistCategory.SONSTIGE
        assert keywords == []

    def test_detect_sonstige_softwareentwickler(self):
        category, keywords = self.service.detect_category("Senior Software Engineer Python")
        assert category == HotlistCategory.SONSTIGE

    # --- Mehrdeutig (mehr Finance als Engineering) ---

    def test_detect_mixed_more_finance(self):
        text = "Buchhalter mit DATEV Erfahrung und etwas Wartung"
        category, keywords = self.service.detect_category(text)
        assert category == HotlistCategory.FINANCE


class TestPLZMapping:
    """Tests für PLZ → Stadt Mapping."""

    def setup_method(self):
        self.service = CategorizationService(db=MagicMock())

    def test_resolve_city_from_city_field(self):
        assert self.service.resolve_city("10115", "Berlin") == "Berlin"

    def test_resolve_city_from_plz_berlin(self):
        assert self.service.resolve_city("10115", None) == "Berlin"

    def test_resolve_city_from_plz_muenchen(self):
        assert self.service.resolve_city("80331", None) == "München"

    def test_resolve_city_from_plz_hamburg(self):
        assert self.service.resolve_city("20095", None) == "Hamburg"

    def test_resolve_city_from_plz_koeln(self):
        assert self.service.resolve_city("50667", None) == "Köln"

    def test_resolve_city_from_plz_frankfurt(self):
        assert self.service.resolve_city("60311", None) == "Frankfurt am Main"

    def test_resolve_city_unknown_plz(self):
        result = self.service.resolve_city("99999", None)
        # 99999 existiert nicht in der vollständigen PLZ-Tabelle
        assert result is None

    def test_resolve_city_real_plz_erfurt(self):
        result = self.service.resolve_city("99084", None)
        assert result == "Erfurt"

    def test_resolve_city_real_plz_small_town(self):
        """Auch kleine Orte müssen gefunden werden."""
        result = self.service.resolve_city("01665", None)
        # Diera-Zehren oder ähnlich — Hauptsache nicht None
        assert result is not None

    def test_resolve_city_padded_plz(self):
        """PLZ mit fehlender führender Null (z.B. 1067 statt 01067)."""
        result = self.service.resolve_city("1067", None)
        assert result == "Dresden"

    def test_resolve_city_no_data(self):
        assert self.service.resolve_city(None, None) is None

    def test_resolve_city_empty_strings(self):
        assert self.service.resolve_city("", "") is None

    def test_resolve_city_prefers_city_field(self):
        """City-Feld hat Priorität vor PLZ-Mapping."""
        result = self.service.resolve_city("10115", "Potsdam")
        assert result == "Potsdam"


class TestJobTitleNormalization:
    """Tests für Job-Title Normalisierung."""

    def setup_method(self):
        self.service = CategorizationService(db=MagicMock())

    def test_normalize_buchhalter(self):
        assert self.service.normalize_job_title("Buchhalter (m/w/d)") == "Buchhalter/in"

    def test_normalize_bilanzbuchhalter(self):
        # "bilanzbuchhalterin" matched vor "buchhalterin" weil es zuerst im Dict steht
        assert self.service.normalize_job_title("Bilanzbuchhalterin IHK") == "Buchhalter/in"  # "buchhalterin" matcht zuerst

    def test_normalize_controller(self):
        assert self.service.normalize_job_title("Senior Controller") == "Controller/in"

    def test_normalize_elektriker(self):
        assert self.service.normalize_job_title("Elektriker gesucht") == "Elektriker/in"

    def test_normalize_servicetechniker(self):
        assert self.service.normalize_job_title("Servicetechniker Kälte") == "Servicetechniker/in"

    def test_normalize_anlagenmechaniker(self):
        assert self.service.normalize_job_title("Anlagenmechaniker SHK") == "Anlagenmechaniker/in SHK"

    def test_normalize_unknown(self):
        assert self.service.normalize_job_title("Projektmanager") is None

    def test_normalize_none(self):
        assert self.service.normalize_job_title(None) is None


# ═══════════════════════════════════════════════════════════════
# PRE-SCORING SERVICE TESTS
# ═══════════════════════════════════════════════════════════════

class TestPreScoring:
    """Tests für die Pre-Score-Berechnung."""

    def setup_method(self):
        self.service = PreScoringService(db=MagicMock())

    def _mock_candidate(self, category="FINANCE", city="Berlin", title="Buchhalter/in"):
        c = MagicMock()
        c.hotlist_category = category
        c.hotlist_city = city
        c.hotlist_job_title = title
        return c

    def _mock_job(self, category="FINANCE", city="Berlin", title="Buchhalter/in"):
        j = MagicMock()
        j.hotlist_category = category
        j.hotlist_city = city
        j.hotlist_job_title = title
        return j

    def _mock_match(self, distance=5.0, keyword_score=0.5, pre_score=None):
        m = MagicMock()
        m.distance_km = distance
        m.keyword_score = keyword_score
        m.pre_score = pre_score
        return m

    def test_perfect_match(self):
        """Perfekter Match: gleiche Kategorie, Stadt, Titel, guter Keyword-Score, kurze Distanz."""
        candidate = self._mock_candidate()
        job = self._mock_job()
        match = self._mock_match(distance=3.0, keyword_score=1.0)

        result = self.service.calculate_pre_score(candidate, job, match)

        assert result.category_score == WEIGHT_CATEGORY  # 30
        assert result.city_score == WEIGHT_CITY           # 25
        assert result.job_title_score == WEIGHT_JOB_TITLE # 20
        assert result.keyword_score == WEIGHT_KEYWORDS    # 15
        assert result.distance_score == WEIGHT_DISTANCE   # 10
        assert result.total == 100.0

    def test_no_match(self):
        """Kein Match: verschiedene Kategorie, verschiedene Stadt/Titel."""
        candidate = self._mock_candidate(category="FINANCE", city="Berlin", title="Buchhalter/in")
        job = self._mock_job(category="ENGINEERING", city="München", title="Elektriker/in")
        match = self._mock_match(distance=30.0, keyword_score=0.0)

        result = self.service.calculate_pre_score(candidate, job, match)

        assert result.category_score == 0.0
        assert result.city_score == 0.0
        assert result.job_title_score == 0.0
        assert result.total == 0.0

    def test_same_category_different_city(self):
        candidate = self._mock_candidate(city="Berlin")
        job = self._mock_job(city="München")
        match = self._mock_match()

        result = self.service.calculate_pre_score(candidate, job, match)

        assert result.category_score == WEIGHT_CATEGORY
        assert result.city_score == 0.0

    def test_distance_scoring_5km(self):
        """≤5km = volle Punkte."""
        candidate = self._mock_candidate()
        job = self._mock_job()
        match = self._mock_match(distance=5.0)

        result = self.service.calculate_pre_score(candidate, job, match)
        assert result.distance_score == WEIGHT_DISTANCE

    def test_distance_scoring_15km(self):
        """15km = halbe Punkte."""
        candidate = self._mock_candidate()
        job = self._mock_job()
        match = self._mock_match(distance=15.0)

        result = self.service.calculate_pre_score(candidate, job, match)
        assert result.distance_score == 5.0  # 10 * (1 - 10/20)

    def test_distance_scoring_25km(self):
        """25km = 0 Punkte."""
        candidate = self._mock_candidate()
        job = self._mock_job()
        match = self._mock_match(distance=25.0)

        result = self.service.calculate_pre_score(candidate, job, match)
        assert result.distance_score == 0.0

    def test_sonstige_no_category_points(self):
        """SONSTIGE bekommt keine Kategorie-Punkte."""
        candidate = self._mock_candidate(category="SONSTIGE")
        job = self._mock_job(category="SONSTIGE")
        match = self._mock_match()

        result = self.service.calculate_pre_score(candidate, job, match)
        assert result.category_score == 0.0

    def test_is_good_match(self):
        result = PreScoreBreakdown(
            category_score=30, city_score=25, job_title_score=0,
            keyword_score=0, distance_score=0, total=55
        )
        assert result.is_good_match is True

    def test_is_not_good_match(self):
        result = PreScoreBreakdown(
            category_score=30, city_score=0, job_title_score=0,
            keyword_score=0, distance_score=0, total=30
        )
        assert result.is_good_match is False


# ═══════════════════════════════════════════════════════════════
# PLZ-MAP VOLLSTÄNDIGKEIT
# ═══════════════════════════════════════════════════════════════

class TestPLZMapCompleteness:
    """Prüft die vollständige PLZ-Map (8.255 Einträge) auf wichtige Städte."""

    def test_plz_map_has_entries(self):
        """Mindestens 8.000 PLZ müssen geladen sein."""
        assert len(PLZ_CITY_MAP) >= 8000

    def test_berlin_mapped(self):
        assert PLZ_CITY_MAP.get("10115") == "Berlin"

    def test_hamburg_mapped(self):
        assert PLZ_CITY_MAP.get("20095") == "Hamburg"

    def test_muenchen_mapped(self):
        assert PLZ_CITY_MAP.get("80331") == "München"

    def test_koeln_mapped(self):
        assert PLZ_CITY_MAP.get("50667") == "Köln"

    def test_frankfurt_mapped(self):
        assert PLZ_CITY_MAP.get("60311") == "Frankfurt am Main"

    def test_stuttgart_mapped(self):
        assert PLZ_CITY_MAP.get("70173") == "Stuttgart"

    def test_duesseldorf_mapped(self):
        assert PLZ_CITY_MAP.get("40213") == "Düsseldorf"

    def test_hannover_mapped(self):
        assert PLZ_CITY_MAP.get("30159") == "Hannover"

    def test_nuernberg_mapped(self):
        assert PLZ_CITY_MAP.get("90402") == "Nürnberg"

    def test_dresden_mapped(self):
        assert PLZ_CITY_MAP.get("01067") == "Dresden"

    def test_leipzig_mapped(self):
        assert PLZ_CITY_MAP.get("04109") == "Leipzig"

    def test_small_town_mapped(self):
        """Auch kleine Orte wie Aach bei Trier müssen enthalten sein."""
        assert PLZ_CITY_MAP.get("54298") == "Aach"

    def test_aachen_mapped(self):
        assert PLZ_CITY_MAP.get("52062") == "Aachen"

    def test_freiburg_mapped(self):
        assert PLZ_CITY_MAP.get("79098") == "Freiburg im Breisgau"


# ═══════════════════════════════════════════════════════════════
# ERWEITERTE TESTS (100 weitere)
# ═══════════════════════════════════════════════════════════════


# ─────────────────────────────────────────────────────────────
# A) FINANCE KEYWORDS — Erweiterte Erkennung (20 Tests)
# ─────────────────────────────────────────────────────────────

class TestFinanceKeywordsExtended:
    """Erweiterte Tests für FINANCE-Kategorie-Erkennung."""

    def setup_method(self):
        self.service = CategorizationService(db=MagicMock())

    def test_finance_finanzbuchhalter(self):
        cat, kw = self.service.detect_category("Finanzbuchhalter (m/w/d)")
        assert cat == HotlistCategory.FINANCE
        assert "finanzbuchhalter" in kw

    def test_finance_lohnbuchhalter(self):
        cat, kw = self.service.detect_category("Lohnbuchhalterin gesucht")
        assert cat == HotlistCategory.FINANCE
        assert "lohnbuchhalterin" in kw

    def test_finance_debitorenbuchhalter(self):
        cat, kw = self.service.detect_category("Debitorenbuchhalter in Teilzeit")
        assert cat == HotlistCategory.FINANCE
        assert "debitorenbuchhalter" in kw

    def test_finance_kreditorenbuchhalter(self):
        cat, kw = self.service.detect_category("Kreditorenbuchhalter (m/w/d)")
        assert cat == HotlistCategory.FINANCE
        assert "kreditorenbuchhalter" in kw

    def test_finance_steuerfachwirt(self):
        cat, kw = self.service.detect_category("Steuerfachwirt mit Berufserfahrung")
        assert cat == HotlistCategory.FINANCE
        assert "steuerfachwirt" in kw

    def test_finance_steuerberater(self):
        cat, kw = self.service.detect_category("Steuerberaterin in eigener Kanzlei")
        assert cat == HotlistCategory.FINANCE
        assert "steuerberaterin" in kw

    def test_finance_wirtschaftspruefer(self):
        cat, kw = self.service.detect_category("Wirtschaftsprüfer / Audit Manager")
        assert cat == HotlistCategory.FINANCE
        assert "wirtschaftsprüfer" in kw

    def test_finance_accountant(self):
        cat, kw = self.service.detect_category("Senior Accountant IFRS")
        assert cat == HotlistCategory.FINANCE
        assert "accountant" in kw

    def test_finance_bilanzierung(self):
        cat, kw = self.service.detect_category("Erfahrung in Bilanzierung nach HGB")
        assert cat == HotlistCategory.FINANCE
        assert "bilanzierung" in kw

    def test_finance_jahresabschluss(self):
        cat, kw = self.service.detect_category("Mitarbeit am Jahresabschluss")
        assert cat == HotlistCategory.FINANCE
        assert "jahresabschluss" in kw

    def test_finance_controlling(self):
        cat, kw = self.service.detect_category("Erfahrung im Controlling und Reporting")
        assert cat == HotlistCategory.FINANCE
        assert "controlling" in kw
        assert "reporting" in kw

    def test_finance_sap_fi(self):
        cat, kw = self.service.detect_category("SAP FI Berater für Finanzwesen")
        assert cat == HotlistCategory.FINANCE
        assert "sap fi" in kw

    def test_finance_lexware(self):
        cat, kw = self.service.detect_category("Buchhaltung mit Lexware")
        assert cat == HotlistCategory.FINANCE
        assert "lexware" in kw

    def test_finance_rechnungswesen(self):
        cat, kw = self.service.detect_category("Sachbearbeiter Rechnungswesen")
        assert cat == HotlistCategory.FINANCE
        assert "rechnungswesen" in kw

    def test_finance_mahnwesen(self):
        cat, kw = self.service.detect_category("Mahnwesen und Zahlungsverkehr")
        assert cat == HotlistCategory.FINANCE
        assert "mahnwesen" in kw
        assert "zahlungsverkehr" in kw

    def test_finance_umsatzsteuer(self):
        cat, kw = self.service.detect_category("Umsatzsteuer-Voranmeldung erstellen")
        assert cat == HotlistCategory.FINANCE
        assert "umsatzsteuer" in kw

    def test_finance_hauptbuchhalter(self):
        cat, kw = self.service.detect_category("Hauptbuchhalter für Konzernabschluss")
        assert cat == HotlistCategory.FINANCE
        assert "hauptbuchhalter" in kw

    def test_finance_accounts_payable(self):
        cat, kw = self.service.detect_category("Accounts Payable Specialist")
        assert cat == HotlistCategory.FINANCE
        assert "accounts payable" in kw

    def test_finance_case_insensitive(self):
        """Keywords müssen case-insensitive matchen."""
        cat, _ = self.service.detect_category("BUCHHALTER IN VOLLZEIT")
        assert cat == HotlistCategory.FINANCE

    def test_finance_multiple_keywords(self):
        """Mehrere Finance-Keywords in einem Text."""
        cat, kw = self.service.detect_category(
            "Buchhalter mit DATEV Erfahrung für Debitorenbuchhaltung und Bilanzierung"
        )
        assert cat == HotlistCategory.FINANCE
        assert len(kw) >= 3


# ─────────────────────────────────────────────────────────────
# B) ENGINEERING KEYWORDS — Erweiterte Erkennung (20 Tests)
# ─────────────────────────────────────────────────────────────

class TestEngineeringKeywordsExtended:
    """Erweiterte Tests für ENGINEERING-Kategorie-Erkennung."""

    def setup_method(self):
        self.service = CategorizationService(db=MagicMock())

    def test_engineering_elektrotechniker(self):
        cat, kw = self.service.detect_category("Elektrotechniker für Industrieanlagen")
        assert cat == HotlistCategory.ENGINEERING
        assert "elektrotechniker" in kw

    def test_engineering_elektroniker(self):
        cat, kw = self.service.detect_category("Elektroniker Betriebstechnik")
        assert cat == HotlistCategory.ENGINEERING
        assert "elektroniker" in kw

    def test_engineering_elektroinstallateur(self):
        cat, kw = self.service.detect_category("Elektroinstallateur für Neubau")
        assert cat == HotlistCategory.ENGINEERING
        assert "elektroinstallateur" in kw

    def test_engineering_industriemechaniker(self):
        cat, kw = self.service.detect_category("Industriemechaniker Instandhaltung")
        assert cat == HotlistCategory.ENGINEERING
        assert "industriemechaniker" in kw

    def test_engineering_kaeltetechniker(self):
        cat, kw = self.service.detect_category("Kältetechniker für Supermarkt-Ketten")
        assert cat == HotlistCategory.ENGINEERING
        assert "kältetechniker" in kw

    def test_engineering_sanitaerinstallateur(self):
        cat, kw = self.service.detect_category("Sanitärinstallateur (m/w/d)")
        assert cat == HotlistCategory.ENGINEERING
        assert "sanitärinstallateur" in kw

    def test_engineering_klempner(self):
        cat, kw = self.service.detect_category("Klempner für Dacharbeiten")
        assert cat == HotlistCategory.ENGINEERING
        assert "klempner" in kw

    def test_engineering_metallbauer(self):
        cat, kw = self.service.detect_category("Metallbauer Konstruktionstechnik")
        assert cat == HotlistCategory.ENGINEERING
        assert "metallbauer" in kw

    def test_engineering_shk_keyword(self):
        cat, kw = self.service.detect_category("Fachkraft SHK mit Erfahrung")
        assert cat == HotlistCategory.ENGINEERING
        assert "shk" in kw

    def test_engineering_photovoltaik(self):
        cat, kw = self.service.detect_category("Monteur für Photovoltaik-Anlagen")
        assert cat == HotlistCategory.ENGINEERING
        assert "photovoltaik" in kw

    def test_engineering_waermepumpe(self):
        cat, kw = self.service.detect_category("Spezialist für Wärmepumpen-Installation")
        assert cat == HotlistCategory.ENGINEERING
        assert "wärmepumpen" in kw

    def test_engineering_sps(self):
        cat, kw = self.service.detect_category("SPS-Programmierung und Steuerungstechnik")
        assert cat == HotlistCategory.ENGINEERING
        assert "sps-programmierung" in kw
        assert "steuerungstechnik" in kw

    def test_engineering_brandschutz(self):
        cat, kw = self.service.detect_category("Brandschutz und Brandmeldeanlagen")
        assert cat == HotlistCategory.ENGINEERING
        assert "brandschutz" in kw

    def test_engineering_gebaeudetechnik(self):
        cat, kw = self.service.detect_category("Fachmann für Gebäudetechnik")
        assert cat == HotlistCategory.ENGINEERING
        assert "gebäudetechnik" in kw

    def test_engineering_schaltschrankbau(self):
        cat, kw = self.service.detect_category("Schaltschrankbau und Verdrahtung")
        assert cat == HotlistCategory.ENGINEERING
        assert "schaltschrankbau" in kw

    def test_engineering_schweissen(self):
        cat, kw = self.service.detect_category("Erfahrung im Schweißen WIG/MAG")
        assert cat == HotlistCategory.ENGINEERING
        assert "schweißen" in kw

    def test_engineering_meisterbrief(self):
        cat, kw = self.service.detect_category("Meisterbrief in Elektrotechnik")
        assert cat == HotlistCategory.ENGINEERING
        assert "meisterbrief" in kw
        assert "elektrotechnik" in kw

    def test_engineering_case_insensitive(self):
        """Keywords müssen case-insensitive matchen."""
        cat, _ = self.service.detect_category("ELEKTRIKER FÜR GEBÄUDETECHNIK")
        assert cat == HotlistCategory.ENGINEERING

    def test_engineering_multiple_keywords(self):
        """Mehrere Engineering-Keywords in einem Text."""
        cat, kw = self.service.detect_category(
            "Servicetechniker Kältetechnik mit Erfahrung in Wartung und Instandhaltung"
        )
        assert cat == HotlistCategory.ENGINEERING
        assert len(kw) >= 3

    def test_engineering_heizungsmonteur(self):
        cat, kw = self.service.detect_category("Heizungsmonteur für Altbausanierung")
        assert cat == HotlistCategory.ENGINEERING
        assert "heizungsmonteur" in kw


# ─────────────────────────────────────────────────────────────
# C) SONSTIGE & Grenzfälle (10 Tests)
# ─────────────────────────────────────────────────────────────

class TestSonstigeAndEdgeCases:
    """Tests für SONSTIGE und Grenzfälle."""

    def setup_method(self):
        self.service = CategorizationService(db=MagicMock())

    def test_sonstige_it_developer(self):
        cat, kw = self.service.detect_category("Java Developer Full Stack")
        assert cat == HotlistCategory.SONSTIGE
        assert kw == []

    def test_sonstige_hr_manager(self):
        cat, kw = self.service.detect_category("HR Manager People & Culture")
        assert cat == HotlistCategory.SONSTIGE
        assert kw == []

    def test_sonstige_sales(self):
        cat, kw = self.service.detect_category("Sales Manager B2B")
        assert cat == HotlistCategory.SONSTIGE
        assert kw == []

    def test_sonstige_arzt(self):
        cat, kw = self.service.detect_category("Facharzt für Innere Medizin")
        assert cat == HotlistCategory.SONSTIGE
        assert kw == []

    def test_sonstige_lehrer(self):
        cat, kw = self.service.detect_category("Gymnasiallehrer Mathematik")
        assert cat == HotlistCategory.SONSTIGE
        assert kw == []

    def test_whitespace_only(self):
        cat, kw = self.service.detect_category("   ")
        assert cat == HotlistCategory.SONSTIGE
        assert kw == []

    def test_special_characters(self):
        cat, kw = self.service.detect_category("!!@@##$$%%")
        assert cat == HotlistCategory.SONSTIGE
        assert kw == []

    def test_mixed_equal_counts_prefers_finance(self):
        """Bei gleicher Trefferanzahl gewinnt FINANCE."""
        cat, _ = self.service.detect_category("Buchhalter mit Wartung")
        assert cat == HotlistCategory.FINANCE

    def test_mixed_more_engineering(self):
        """Engineering gewinnt bei mehr Engineering-Keywords."""
        cat, _ = self.service.detect_category(
            "Servicetechniker Kältetechnik Wartung Instandhaltung und etwas Buchhaltung"
        )
        assert cat == HotlistCategory.ENGINEERING

    def test_detect_very_long_text(self):
        """Langer Text mit einem Finance-Keyword am Ende."""
        long_text = "Dies ist ein sehr langer Beschreibungstext. " * 50 + "Buchhalter"
        cat, kw = self.service.detect_category(long_text)
        assert cat == HotlistCategory.FINANCE
        assert "buchhalter" in kw


# ─────────────────────────────────────────────────────────────
# D) PLZ → Stadt Mapping Erweitert (15 Tests)
# ─────────────────────────────────────────────────────────────

class TestPLZMappingExtended:
    """Erweiterte PLZ-Mapping Tests."""

    def setup_method(self):
        self.service = CategorizationService(db=MagicMock())

    def test_plz_dortmund(self):
        assert self.service.resolve_city("44135", None) is not None

    def test_plz_essen(self):
        assert self.service.resolve_city("45127", None) is not None

    def test_plz_bremen(self):
        assert self.service.resolve_city("28195", None) is not None

    def test_plz_duisburg(self):
        assert self.service.resolve_city("47051", None) is not None

    def test_plz_bochum(self):
        assert self.service.resolve_city("44787", None) is not None

    def test_plz_wuppertal(self):
        assert self.service.resolve_city("42103", None) is not None

    def test_plz_bonn(self):
        assert self.service.resolve_city("53111", None) is not None

    def test_plz_muenster(self):
        assert self.service.resolve_city("48143", None) is not None

    def test_plz_karlsruhe(self):
        assert self.service.resolve_city("76131", None) is not None

    def test_plz_mannheim(self):
        assert self.service.resolve_city("68159", None) is not None

    def test_plz_augsburg(self):
        assert self.service.resolve_city("86150", None) is not None

    def test_plz_wiesbaden(self):
        assert self.service.resolve_city("65183", None) is not None

    def test_padded_plz_3_digits(self):
        """3-stellige PLZ werden NICHT aufgefüllt (nur 4-stellige)."""
        result = self.service.resolve_city("106", None)
        assert result is None

    def test_plz_with_whitespace(self):
        """PLZ mit Leerzeichen davor/danach."""
        result = self.service.resolve_city(" 10115 ", None)
        assert result == "Berlin"

    def test_city_field_with_whitespace(self):
        """City-Feld mit Leerzeichen wird getrimmt."""
        result = self.service.resolve_city(None, " München ")
        assert result == "München"


# ─────────────────────────────────────────────────────────────
# E) Job-Title Normalisierung Erweitert (15 Tests)
# ─────────────────────────────────────────────────────────────

class TestJobTitleNormalizationExtended:
    """Erweiterte Tests für Job-Title Normalisierung."""

    def setup_method(self):
        self.service = CategorizationService(db=MagicMock())

    def test_normalize_finanzbuchhalter(self):
        # "buchhalter" matcht zuerst im Dict (Iteration Order)
        assert self.service.normalize_job_title("Finanzbuchhalter (m/w/d)") == "Buchhalter/in"

    def test_normalize_lohnbuchhalter(self):
        # "buchhalter" matcht zuerst im Dict (Iteration Order)
        assert self.service.normalize_job_title("Lohnbuchhalter gesucht") == "Buchhalter/in"

    def test_normalize_steuerfachangestellte(self):
        assert self.service.normalize_job_title("Steuerfachangestellte Kanzlei") == "Steuerfachangestellte/r"

    def test_normalize_steuerberater(self):
        assert self.service.normalize_job_title("Steuerberater (m/w/d)") == "Steuerberater/in"

    def test_normalize_wirtschaftspruefer(self):
        assert self.service.normalize_job_title("Wirtschaftsprüfer Senior") == "Wirtschaftsprüfer/in"

    def test_normalize_elektroniker(self):
        assert self.service.normalize_job_title("Elektroniker Betriebstechnik") == "Elektroniker/in"

    def test_normalize_mechatroniker(self):
        assert self.service.normalize_job_title("Mechatroniker Instandhaltung") == "Mechatroniker/in"

    def test_normalize_industriemechaniker(self):
        assert self.service.normalize_job_title("Industriemechaniker") == "Industriemechaniker/in"

    def test_normalize_kaeltetechniker(self):
        assert self.service.normalize_job_title("Kältetechniker") == "Kältetechniker/in"

    def test_normalize_heizungsmonteur(self):
        assert self.service.normalize_job_title("Heizungsmonteur Vollzeit") == "Heizungsmonteur/in"

    def test_normalize_sanitaerinstallateur(self):
        assert self.service.normalize_job_title("Sanitärinstallateur") == "Sanitärinstallateur/in"

    def test_normalize_klempner(self):
        assert self.service.normalize_job_title("Klempner gesucht") == "Klempner/in"

    def test_normalize_schlosser(self):
        assert self.service.normalize_job_title("Schlosser MIG/MAG") == "Schlosser/in"

    def test_normalize_metallbauer(self):
        assert self.service.normalize_job_title("Metallbauer Konstruktion") == "Metallbauer/in"

    def test_normalize_empty_string(self):
        assert self.service.normalize_job_title("") is None


# ─────────────────────────────────────────────────────────────
# F) Pre-Scoring — Detailszenarien (20 Tests)
# ─────────────────────────────────────────────────────────────

class TestPreScoringExtended:
    """Erweiterte Pre-Scoring Tests — Grenzwerte, Teilscores, Randfälle."""

    def setup_method(self):
        self.service = PreScoringService(db=MagicMock())

    def _mc(self, category="FINANCE", city="Berlin", title="Buchhalter/in"):
        c = MagicMock()
        c.hotlist_category = category
        c.hotlist_city = city
        c.hotlist_job_title = title
        return c

    def _mj(self, category="FINANCE", city="Berlin", title="Buchhalter/in"):
        j = MagicMock()
        j.hotlist_category = category
        j.hotlist_city = city
        j.hotlist_job_title = title
        return j

    def _mm(self, distance=5.0, keyword_score=0.5):
        m = MagicMock()
        m.distance_km = distance
        m.keyword_score = keyword_score
        m.pre_score = None
        return m

    # --- Distanz-Grenzwerte ---

    def test_distance_0km(self):
        """0km = volle Punkte."""
        r = self.service.calculate_pre_score(self._mc(), self._mj(), self._mm(distance=0.0))
        assert r.distance_score == WEIGHT_DISTANCE

    def test_distance_exactly_5km(self):
        """Exakt 5km = volle Punkte."""
        r = self.service.calculate_pre_score(self._mc(), self._mj(), self._mm(distance=5.0))
        assert r.distance_score == WEIGHT_DISTANCE

    def test_distance_5_5km(self):
        """5.5km = knapp unter volle Punkte."""
        r = self.service.calculate_pre_score(self._mc(), self._mj(), self._mm(distance=5.5))
        assert r.distance_score < WEIGHT_DISTANCE
        assert r.distance_score > 9.0

    def test_distance_10km(self):
        """10km = 7.5 Punkte."""
        r = self.service.calculate_pre_score(self._mc(), self._mj(), self._mm(distance=10.0))
        assert r.distance_score == 7.5

    def test_distance_20km(self):
        """20km = 2.5 Punkte."""
        r = self.service.calculate_pre_score(self._mc(), self._mj(), self._mm(distance=20.0))
        assert r.distance_score == 2.5

    def test_distance_exactly_25km(self):
        """Exakt 25km = 0 Punkte."""
        r = self.service.calculate_pre_score(self._mc(), self._mj(), self._mm(distance=25.0))
        assert r.distance_score == 0.0

    def test_distance_50km(self):
        """50km = 0 Punkte (weit über 25km)."""
        r = self.service.calculate_pre_score(self._mc(), self._mj(), self._mm(distance=50.0))
        assert r.distance_score == 0.0

    def test_distance_none(self):
        """distance_km = None → 0 Punkte."""
        m = MagicMock()
        m.distance_km = None
        m.keyword_score = 0.5
        r = self.service.calculate_pre_score(self._mc(), self._mj(), m)
        assert r.distance_score == 0.0

    # --- Keyword-Score Grenzwerte ---

    def test_keyword_score_0(self):
        """keyword_score = 0 → 0 Punkte."""
        r = self.service.calculate_pre_score(self._mc(), self._mj(), self._mm(keyword_score=0.0))
        assert r.keyword_score == 0.0

    def test_keyword_score_1(self):
        """keyword_score = 1.0 → volle 15 Punkte."""
        r = self.service.calculate_pre_score(self._mc(), self._mj(), self._mm(keyword_score=1.0))
        assert r.keyword_score == WEIGHT_KEYWORDS

    def test_keyword_score_half(self):
        """keyword_score = 0.5 → 7.5 Punkte."""
        r = self.service.calculate_pre_score(self._mc(), self._mj(), self._mm(keyword_score=0.5))
        assert r.keyword_score == 7.5

    def test_keyword_score_above_1_capped(self):
        """keyword_score > 1.0 wird auf 1.0 gecappt."""
        r = self.service.calculate_pre_score(self._mc(), self._mj(), self._mm(keyword_score=2.0))
        assert r.keyword_score == WEIGHT_KEYWORDS

    def test_keyword_score_none(self):
        """keyword_score = None → 0 Punkte."""
        m = MagicMock()
        m.distance_km = 5.0
        m.keyword_score = None
        r = self.service.calculate_pre_score(self._mc(), self._mj(), m)
        assert r.keyword_score == 0.0

    # --- Kategorie-Szenarien ---

    def test_engineering_match(self):
        """ENGINEERING-Kategorie matcht korrekt."""
        c = self._mc(category="ENGINEERING", city="Berlin", title="Elektriker/in")
        j = self._mj(category="ENGINEERING", city="Berlin", title="Elektriker/in")
        r = self.service.calculate_pre_score(c, j, self._mm())
        assert r.category_score == WEIGHT_CATEGORY

    def test_category_none_candidate(self):
        """Kandidat ohne Kategorie → 0 Kategorie-Punkte."""
        c = self._mc(category=None)
        j = self._mj(category="FINANCE")
        r = self.service.calculate_pre_score(c, j, self._mm())
        assert r.category_score == 0.0

    def test_category_none_job(self):
        """Job ohne Kategorie → 0 Kategorie-Punkte."""
        c = self._mc(category="FINANCE")
        j = self._mj(category=None)
        r = self.service.calculate_pre_score(c, j, self._mm())
        assert r.category_score == 0.0

    # --- Stadt-Szenarien ---

    def test_city_case_insensitive(self):
        """Stadt-Vergleich ist case-insensitive."""
        c = self._mc(city="berlin")
        j = self._mj(city="BERLIN")
        r = self.service.calculate_pre_score(c, j, self._mm())
        assert r.city_score == WEIGHT_CITY

    def test_city_none_candidate(self):
        """Kandidat ohne Stadt → 0 Stadt-Punkte."""
        c = self._mc(city=None)
        j = self._mj(city="Berlin")
        r = self.service.calculate_pre_score(c, j, self._mm())
        assert r.city_score == 0.0

    def test_city_none_job(self):
        """Job ohne Stadt → 0 Stadt-Punkte."""
        c = self._mc(city="Berlin")
        j = self._mj(city=None)
        r = self.service.calculate_pre_score(c, j, self._mm())
        assert r.city_score == 0.0

    # --- Job-Title-Szenarien ---

    def test_title_case_insensitive(self):
        """Titel-Vergleich ist case-insensitive."""
        c = self._mc(title="buchhalter/in")
        j = self._mj(title="Buchhalter/in")
        r = self.service.calculate_pre_score(c, j, self._mm())
        assert r.job_title_score == WEIGHT_JOB_TITLE


# ─────────────────────────────────────────────────────────────
# G) PreScoreBreakdown Dataclass (5 Tests)
# ─────────────────────────────────────────────────────────────

class TestPreScoreBreakdownDataclass:
    """Tests für PreScoreBreakdown Datenklasse."""

    def test_total_calculation_manual(self):
        b = PreScoreBreakdown(
            category_score=30, city_score=25, job_title_score=20,
            keyword_score=15, distance_score=10, total=100
        )
        assert b.total == 100.0

    def test_is_good_match_exact_50(self):
        """Exakt 50 = guter Match (>=50)."""
        b = PreScoreBreakdown(
            category_score=30, city_score=20, job_title_score=0,
            keyword_score=0, distance_score=0, total=50
        )
        assert b.is_good_match is True

    def test_is_good_match_49(self):
        """49.9 = kein guter Match (<50)."""
        b = PreScoreBreakdown(
            category_score=30, city_score=19.9, job_title_score=0,
            keyword_score=0, distance_score=0, total=49.9
        )
        assert b.is_good_match is False

    def test_is_good_match_zero(self):
        """0 = kein guter Match."""
        b = PreScoreBreakdown(
            category_score=0, city_score=0, job_title_score=0,
            keyword_score=0, distance_score=0, total=0
        )
        assert b.is_good_match is False

    def test_is_good_match_100(self):
        """100 = guter Match."""
        b = PreScoreBreakdown(
            category_score=30, city_score=25, job_title_score=20,
            keyword_score=15, distance_score=10, total=100
        )
        assert b.is_good_match is True


# ─────────────────────────────────────────────────────────────
# H) PLZ-Map Datenqualität (10 Tests)
# ─────────────────────────────────────────────────────────────

class TestPLZMapDataQuality:
    """Tests für die Datenqualität der PLZ-Map."""

    def test_all_keys_are_5_digit_strings(self):
        """Alle PLZ-Schlüssel müssen 5-stellige Strings sein."""
        for plz in PLZ_CITY_MAP:
            assert len(plz) == 5, f"PLZ {plz} ist nicht 5-stellig"
            assert plz.isdigit(), f"PLZ {plz} enthält Nicht-Ziffern"

    def test_all_values_are_non_empty_strings(self):
        """Alle Städtenamen müssen nicht-leere Strings sein."""
        for plz, city in PLZ_CITY_MAP.items():
            assert isinstance(city, str), f"PLZ {plz}: Stadt ist kein String"
            assert len(city) > 0, f"PLZ {plz}: Stadtname ist leer"

    def test_no_duplicate_plz(self):
        """Keine doppelten PLZ (automatisch durch dict, aber explizit prüfen)."""
        assert len(PLZ_CITY_MAP) == len(set(PLZ_CITY_MAP.keys()))

    def test_berlin_has_multiple_plz(self):
        """Berlin muss mehrere PLZ haben (10115, 10117, ...)."""
        berlin_plz = [plz for plz, city in PLZ_CITY_MAP.items() if city == "Berlin"]
        assert len(berlin_plz) >= 10

    def test_hamburg_has_multiple_plz(self):
        """Hamburg muss mehrere PLZ haben."""
        hamburg_plz = [plz for plz, city in PLZ_CITY_MAP.items() if city == "Hamburg"]
        assert len(hamburg_plz) >= 5

    def test_muenchen_has_multiple_plz(self):
        """München muss mehrere PLZ haben."""
        muenchen_plz = [plz for plz, city in PLZ_CITY_MAP.items() if city == "München"]
        assert len(muenchen_plz) >= 5

    def test_frankfurt_corrected(self):
        """Frankfurt-PLZ müssen als 'Frankfurt am Main' gespeichert sein."""
        for plz, city in PLZ_CITY_MAP.items():
            if city == "Frankfurt":
                pytest.fail(f"PLZ {plz} hat 'Frankfurt' statt 'Frankfurt am Main'")

    def test_freiburg_corrected(self):
        """Freiburg-PLZ müssen als 'Freiburg im Breisgau' gespeichert sein."""
        for plz, city in PLZ_CITY_MAP.items():
            if city == "Freiburg":
                pytest.fail(f"PLZ {plz} hat 'Freiburg' statt 'Freiburg im Breisgau'")

    def test_offenbach_corrected(self):
        """Offenbach-PLZ müssen als 'Offenbach am Main' gespeichert sein."""
        for plz, city in PLZ_CITY_MAP.items():
            if city == "Offenbach":
                pytest.fail(f"PLZ {plz} hat 'Offenbach' statt 'Offenbach am Main'")

    def test_plz_map_range_coverage(self):
        """PLZ aus allen Bereichen 0xxxx–9xxxx müssen vorhanden sein."""
        for prefix in ["0", "1", "2", "3", "4", "5", "6", "7", "8", "9"]:
            matching = [plz for plz in PLZ_CITY_MAP if plz.startswith(prefix)]
            assert len(matching) >= 100, f"Zu wenige PLZ mit Präfix {prefix}: {len(matching)}"


# ─────────────────────────────────────────────────────────────
# I) CategorizationResult & BatchCategorizationResult (5 Tests)
# ─────────────────────────────────────────────────────────────

class TestDataclasses:
    """Tests für Dataclass-Strukturen."""

    def test_categorization_result_fields(self):
        from app.services.categorization_service import CategorizationResult
        r = CategorizationResult(
            category="FINANCE", city="Berlin",
            job_title="Buchhalter/in", matched_keywords=["buchhalter"]
        )
        assert r.category == "FINANCE"
        assert r.city == "Berlin"
        assert r.job_title == "Buchhalter/in"
        assert r.matched_keywords == ["buchhalter"]

    def test_categorization_result_none_fields(self):
        from app.services.categorization_service import CategorizationResult
        r = CategorizationResult(
            category="SONSTIGE", city=None,
            job_title=None, matched_keywords=[]
        )
        assert r.city is None
        assert r.job_title is None

    def test_batch_categorization_result(self):
        from app.services.categorization_service import BatchCategorizationResult
        b = BatchCategorizationResult(
            total=100, categorized=95,
            finance=40, engineering=35, sonstige=20, skipped=5
        )
        assert b.total == 100
        assert b.categorized == 95
        assert b.skipped == 5

    def test_pre_scoring_result(self):
        from app.services.pre_scoring_service import PreScoringResult
        r = PreScoringResult(
            total_matches=50, scored=48,
            skipped=2, avg_score=65.3
        )
        assert r.total_matches == 50
        assert r.avg_score == 65.3

    def test_deepmatch_result_success(self):
        from app.services.deepmatch_service import DeepMatchResult
        from uuid import uuid4
        r = DeepMatchResult(
            match_id=uuid4(),
            candidate_name="Max Mustermann",
            job_position="Buchhalter",
            ai_score=0.85,
            explanation="Gute Passung",
            strengths=["DATEV", "HGB"],
            weaknesses=["Keine IFRS-Erfahrung"],
            success=True,
        )
        assert r.success is True
        assert r.ai_score == 0.85
        assert len(r.strengths) == 2

    def test_deepmatch_result_failure(self):
        from app.services.deepmatch_service import DeepMatchResult
        from uuid import uuid4
        r = DeepMatchResult(
            match_id=uuid4(),
            candidate_name="Fehler",
            job_position="Fehler",
            ai_score=0.0,
            explanation="Fehler",
            strengths=[],
            weaknesses=[],
            success=False,
            error="Match nicht gefunden",
        )
        assert r.success is False
        assert r.error == "Match nicht gefunden"

    def test_deepmatch_batch_result(self):
        from app.services.deepmatch_service import DeepMatchBatchResult
        b = DeepMatchBatchResult(
            total_requested=10,
            evaluated=8,
            skipped_low_score=1,
            skipped_error=1,
            avg_ai_score=0.72,
            results=[],
            total_cost_usd=0.05,
        )
        assert b.total_requested == 10
        assert b.evaluated == 8
        assert b.avg_ai_score == 0.72

    def test_hotlist_category_constants(self):
        assert HotlistCategory.FINANCE == "FINANCE"
        assert HotlistCategory.ENGINEERING == "ENGINEERING"
        assert HotlistCategory.SONSTIGE == "SONSTIGE"

    def test_weight_constants_sum_to_100(self):
        """Alle Gewichtungen müssen zusammen 100 ergeben."""
        total = WEIGHT_CATEGORY + WEIGHT_CITY + WEIGHT_JOB_TITLE + WEIGHT_KEYWORDS + WEIGHT_DISTANCE
        assert total == 100.0

    def test_deepmatch_threshold(self):
        from app.services.deepmatch_service import DEEPMATCH_PRE_SCORE_THRESHOLD
        assert DEEPMATCH_PRE_SCORE_THRESHOLD == 40.0
