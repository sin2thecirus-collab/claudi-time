"""Tests für Validierungen."""

import uuid

import pytest
from pydantic import ValidationError

from app.config import Limits
from app.schemas.validators import (
    BatchDeleteRequest,
    BatchHideRequest,
    validate_city,
    validate_postal_code,
    validate_search_term,
    validate_skills_list,
    validate_uuid_list,
)


class TestPostalCodeValidation:
    """Tests für PLZ-Validierung."""

    def test_valid_postal_code(self):
        """Gültige PLZ wird akzeptiert."""
        assert validate_postal_code("20095") == "20095"
        assert validate_postal_code("80331") == "80331"
        assert validate_postal_code("01067") == "01067"

    def test_postal_code_with_whitespace(self):
        """PLZ mit Whitespace wird getrimmt."""
        assert validate_postal_code("  20095  ") == "20095"
        assert validate_postal_code(" 80331") == "80331"

    def test_none_postal_code(self):
        """None wird akzeptiert."""
        assert validate_postal_code(None) is None

    def test_empty_postal_code(self):
        """Leere Strings werden zu None."""
        assert validate_postal_code("") is None
        assert validate_postal_code("   ") is None

    def test_invalid_postal_code_too_short(self):
        """Zu kurze PLZ wird abgelehnt."""
        with pytest.raises(ValueError, match="5 Ziffern"):
            validate_postal_code("2009")

    def test_invalid_postal_code_too_long(self):
        """Zu lange PLZ wird abgelehnt."""
        with pytest.raises(ValueError, match="5 Ziffern"):
            validate_postal_code("200950")

    def test_invalid_postal_code_with_letters(self):
        """PLZ mit Buchstaben wird abgelehnt."""
        with pytest.raises(ValueError, match="5 Ziffern"):
            validate_postal_code("2009A")

    def test_invalid_postal_code_format(self):
        """PLZ mit Sonderzeichen wird abgelehnt."""
        with pytest.raises(ValueError, match="5 Ziffern"):
            validate_postal_code("20-095")


class TestCityValidation:
    """Tests für Städte-Validierung."""

    def test_valid_city(self):
        """Gültiger Stadtname wird akzeptiert."""
        assert validate_city("Hamburg") == "Hamburg"
        assert validate_city("München") == "München"
        assert validate_city("Frankfurt am Main") == "Frankfurt am Main"

    def test_city_with_whitespace(self):
        """Stadt mit Whitespace wird getrimmt."""
        assert validate_city("  Hamburg  ") == "Hamburg"

    def test_none_city(self):
        """None wird akzeptiert."""
        assert validate_city(None) is None

    def test_empty_city(self):
        """Leere Strings werden zu None."""
        assert validate_city("") is None
        assert validate_city("   ") is None

    def test_city_too_short(self):
        """Zu kurze Stadt wird abgelehnt."""
        with pytest.raises(ValueError, match="mindestens 2 Zeichen"):
            validate_city("A")

    def test_city_too_long(self):
        """Zu lange Stadt wird abgelehnt."""
        with pytest.raises(ValueError, match="maximal 100 Zeichen"):
            validate_city("A" * 101)

    def test_city_max_length(self):
        """Stadt mit genau 100 Zeichen wird akzeptiert."""
        city = "A" * 100
        assert validate_city(city) == city


class TestSearchTermValidation:
    """Tests für Suchbegriff-Validierung."""

    def test_valid_search_term(self):
        """Gültiger Suchbegriff wird akzeptiert."""
        assert validate_search_term("Buchhalter") == "Buchhalter"
        assert validate_search_term("SAP") == "SAP"

    def test_search_term_with_whitespace(self):
        """Suchbegriff mit Whitespace wird getrimmt."""
        assert validate_search_term("  Buchhalter  ") == "Buchhalter"

    def test_none_search_term(self):
        """None wird akzeptiert."""
        assert validate_search_term(None) is None

    def test_empty_search_term(self):
        """Leere Strings werden zu None."""
        assert validate_search_term("") is None
        assert validate_search_term("   ") is None

    def test_search_term_too_short(self):
        """Zu kurzer Suchbegriff wird abgelehnt."""
        with pytest.raises(ValueError, match="mindestens"):
            validate_search_term("A")

    def test_search_term_too_long(self):
        """Zu langer Suchbegriff wird abgelehnt."""
        with pytest.raises(ValueError, match="maximal"):
            validate_search_term("A" * (Limits.SEARCH_MAX_LENGTH + 1))

    def test_search_term_min_length(self):
        """Suchbegriff mit Mindestlänge wird akzeptiert."""
        term = "A" * Limits.SEARCH_MIN_LENGTH
        assert validate_search_term(term) == term


class TestUUIDListValidation:
    """Tests für UUID-Listen-Validierung."""

    def test_valid_uuid_list(self):
        """Gültige UUID-Liste wird akzeptiert."""
        uuids = [uuid.uuid4(), uuid.uuid4()]
        result = validate_uuid_list(uuids)
        assert result == uuids

    def test_empty_uuid_list(self):
        """Leere Liste wird abgelehnt."""
        with pytest.raises(ValueError, match="nicht leer"):
            validate_uuid_list([])

    def test_uuid_list_too_long(self):
        """Zu lange Liste wird abgelehnt."""
        uuids = [uuid.uuid4() for _ in range(Limits.BATCH_DELETE_MAX + 1)]
        with pytest.raises(ValueError, match="Maximal"):
            validate_uuid_list(uuids)

    def test_uuid_list_max_length(self):
        """Liste mit Maximallänge wird akzeptiert."""
        uuids = [uuid.uuid4() for _ in range(Limits.BATCH_DELETE_MAX)]
        result = validate_uuid_list(uuids)
        assert len(result) == Limits.BATCH_DELETE_MAX


class TestSkillsListValidation:
    """Tests für Skills-Listen-Validierung."""

    def test_valid_skills_list(self):
        """Gültige Skills-Liste wird akzeptiert."""
        skills = ["SAP", "DATEV", "Buchhaltung"]
        result = validate_skills_list(skills)
        assert result == skills

    def test_none_skills_list(self):
        """None wird akzeptiert."""
        assert validate_skills_list(None) is None

    def test_skills_list_with_whitespace(self):
        """Skills werden getrimmt."""
        skills = ["  SAP  ", " DATEV ", "  Buchhaltung  "]
        result = validate_skills_list(skills)
        assert result == ["SAP", "DATEV", "Buchhaltung"]

    def test_skills_list_filters_empty(self):
        """Leere Skills werden gefiltert."""
        skills = ["SAP", "", "  ", "DATEV"]
        result = validate_skills_list(skills)
        assert result == ["SAP", "DATEV"]

    def test_skills_list_too_long(self):
        """Zu lange Liste wird abgelehnt."""
        skills = [f"Skill{i}" for i in range(Limits.FILTER_MULTI_SELECT_MAX + 1)]
        with pytest.raises(ValueError, match="Maximal"):
            validate_skills_list(skills)


class TestBatchDeleteRequest:
    """Tests für BatchDeleteRequest Schema."""

    def test_valid_batch_delete(self):
        """Gültige Batch-Delete-Anfrage wird akzeptiert."""
        uuids = [uuid.uuid4(), uuid.uuid4()]
        request = BatchDeleteRequest(ids=uuids)
        assert request.ids == uuids

    def test_empty_batch_delete(self):
        """Leere Batch-Delete-Anfrage wird abgelehnt."""
        with pytest.raises(ValidationError):
            BatchDeleteRequest(ids=[])

    def test_batch_delete_over_limit(self):
        """Zu große Batch-Delete-Anfrage wird abgelehnt."""
        uuids = [uuid.uuid4() for _ in range(Limits.BATCH_DELETE_MAX + 1)]
        with pytest.raises(ValidationError):
            BatchDeleteRequest(ids=uuids)


class TestBatchHideRequest:
    """Tests für BatchHideRequest Schema."""

    def test_valid_batch_hide(self):
        """Gültige Batch-Hide-Anfrage wird akzeptiert."""
        uuids = [uuid.uuid4(), uuid.uuid4()]
        request = BatchHideRequest(ids=uuids)
        assert request.ids == uuids

    def test_empty_batch_hide(self):
        """Leere Batch-Hide-Anfrage wird abgelehnt."""
        with pytest.raises(ValidationError):
            BatchHideRequest(ids=[])

    def test_batch_hide_over_limit(self):
        """Zu große Batch-Hide-Anfrage wird abgelehnt."""
        uuids = [uuid.uuid4() for _ in range(Limits.BATCH_HIDE_MAX + 1)]
        with pytest.raises(ValidationError):
            BatchHideRequest(ids=uuids)
