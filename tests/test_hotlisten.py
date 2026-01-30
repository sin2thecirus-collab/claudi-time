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
