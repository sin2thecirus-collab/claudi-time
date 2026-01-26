"""Tests für OpenAI Service mit Mocks."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.openai_service import (
    MATCHING_SYSTEM_PROMPT,
    PRICE_INPUT_PER_1M,
    PRICE_OUTPUT_PER_1M,
    MatchEvaluation,
    OpenAIService,
    OpenAIUsage,
)


class TestOpenAIUsage:
    """Tests für OpenAIUsage Datenklasse."""

    def test_cost_calculation(self):
        """Kosten werden korrekt berechnet."""
        usage = OpenAIUsage(
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            total_tokens=2_000_000,
        )

        expected_cost = PRICE_INPUT_PER_1M + PRICE_OUTPUT_PER_1M
        assert abs(usage.cost_usd - expected_cost) < 0.0001

    def test_cost_zero_tokens(self):
        """Null Tokens = Null Kosten."""
        usage = OpenAIUsage()
        assert usage.cost_usd == 0.0

    def test_cost_small_usage(self):
        """Kleine Token-Anzahl wird korrekt berechnet."""
        usage = OpenAIUsage(
            input_tokens=1000,
            output_tokens=500,
            total_tokens=1500,
        )
        # 1000 input tokens = $0.00015
        # 500 output tokens = $0.0003
        expected = (1000 / 1_000_000) * PRICE_INPUT_PER_1M + (500 / 1_000_000) * PRICE_OUTPUT_PER_1M
        assert abs(usage.cost_usd - expected) < 0.000001


class TestMatchEvaluation:
    """Tests für MatchEvaluation Datenklasse."""

    def test_successful_evaluation(self):
        """Erfolgreiche Bewertung hat korrekte Attribute."""
        evaluation = MatchEvaluation(
            score=0.85,
            explanation="Gute Passung",
            strengths=["Erfahrung", "Skills"],
            weaknesses=["Branchenwechsel"],
            usage=OpenAIUsage(input_tokens=800, output_tokens=150),
            success=True,
            source="openai",
        )

        assert evaluation.score == 0.85
        assert evaluation.success is True
        assert evaluation.source == "openai"
        assert evaluation.error is None

    def test_fallback_evaluation(self):
        """Fallback-Bewertung markiert Fehler."""
        evaluation = MatchEvaluation(
            score=0.3,
            explanation="Fallback",
            strengths=[],
            weaknesses=["KI nicht verfügbar"],
            usage=OpenAIUsage(),
            success=False,
            error="Timeout",
            source="fallback",
        )

        assert evaluation.success is False
        assert evaluation.source == "fallback"
        assert evaluation.error == "Timeout"


class TestOpenAIService:
    """Tests für OpenAIService mit Mocks."""

    def test_is_configured_with_key(self):
        """Service ist konfiguriert wenn API-Key vorhanden."""
        service = OpenAIService(api_key="test-key")
        assert service.is_configured is True

    def test_is_not_configured_without_key(self):
        """Service ist nicht konfiguriert ohne API-Key."""
        with patch("app.services.openai_service.settings") as mock_settings:
            mock_settings.openai_api_key = None
            service = OpenAIService()
            assert service.is_configured is False

    @pytest.mark.asyncio
    async def test_evaluate_match_without_key_returns_fallback(self):
        """Ohne API-Key wird Fallback-Bewertung zurückgegeben."""
        with patch("app.services.openai_service.settings") as mock_settings:
            mock_settings.openai_api_key = None
            service = OpenAIService()

            result = await service.evaluate_match(
                job_data={"position": "Buchhalter", "company_name": "Test GmbH"},
                candidate_data={"full_name": "Max Mustermann", "skills": ["SAP"]},
            )

            assert result.success is False
            assert result.source == "fallback"
            assert "nicht konfiguriert" in result.error

    @pytest.mark.asyncio
    async def test_evaluate_match_success(self):
        """Erfolgreicher API-Aufruf."""
        mock_response = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps({
                            "score": 0.85,
                            "explanation": "Gute Passung aufgrund der SAP-Kenntnisse.",
                            "strengths": ["SAP-Erfahrung", "Buchhaltungskenntnisse"],
                            "weaknesses": ["Branchenwechsel erforderlich"],
                        })
                    }
                }
            ],
            "usage": {
                "prompt_tokens": 800,
                "completion_tokens": 150,
                "total_tokens": 950,
            },
        }

        service = OpenAIService(api_key="test-key")

        with patch.object(service, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_response_obj = MagicMock()
            mock_response_obj.json.return_value = mock_response
            mock_response_obj.raise_for_status = MagicMock()
            mock_client.post.return_value = mock_response_obj
            mock_get_client.return_value = mock_client

            result = await service.evaluate_match(
                job_data={
                    "position": "Buchhalter",
                    "company_name": "Test GmbH",
                    "job_text": "Wir suchen einen erfahrenen Buchhalter mit SAP.",
                },
                candidate_data={
                    "full_name": "Max Mustermann",
                    "skills": ["SAP", "DATEV", "Buchhaltung"],
                    "current_position": "Finanzbuchhalter",
                },
            )

            assert result.success is True
            assert result.source == "openai"
            assert result.score == 0.85
            assert "SAP" in str(result.strengths)
            assert result.usage.total_tokens == 950

    @pytest.mark.asyncio
    async def test_evaluate_match_timeout_fallback(self):
        """Bei Timeout wird Fallback verwendet."""
        service = OpenAIService(api_key="test-key")

        with patch.object(service, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.post.side_effect = httpx.TimeoutException("Timeout")
            mock_get_client.return_value = mock_client

            result = await service.evaluate_match(
                job_data={"position": "Buchhalter", "job_text": "SAP DATEV"},
                candidate_data={"full_name": "Test", "skills": ["SAP", "DATEV"]},
                retry_count=1,  # Weniger Retries für schnelleren Test
            )

            assert result.success is False
            assert result.source == "fallback"
            assert "Timeout" in result.error

    @pytest.mark.asyncio
    async def test_evaluate_match_http_error_fallback(self):
        """Bei HTTP-Fehler wird Fallback verwendet."""
        service = OpenAIService(api_key="test-key")

        with patch.object(service, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.status_code = 429
            mock_client.post.side_effect = httpx.HTTPStatusError(
                "Rate limited", request=MagicMock(), response=mock_response
            )
            mock_get_client.return_value = mock_client

            result = await service.evaluate_match(
                job_data={"position": "Test"},
                candidate_data={"skills": []},
            )

            assert result.success is False
            assert result.source == "fallback"
            assert "429" in result.error

    @pytest.mark.asyncio
    async def test_evaluate_match_json_error_fallback(self):
        """Bei ungültigem JSON wird Fallback verwendet."""
        mock_response = {
            "choices": [
                {
                    "message": {
                        "content": "Dies ist kein JSON"
                    }
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        }

        service = OpenAIService(api_key="test-key")

        with patch.object(service, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_response_obj = MagicMock()
            mock_response_obj.json.return_value = mock_response
            mock_response_obj.raise_for_status = MagicMock()
            mock_client.post.return_value = mock_response_obj
            mock_get_client.return_value = mock_client

            result = await service.evaluate_match(
                job_data={"position": "Test"},
                candidate_data={"skills": []},
            )

            assert result.success is False
            assert result.source == "fallback"
            assert "Ungültige" in result.error or "JSON" in result.error


class TestFallbackEvaluation:
    """Tests für Fallback-Bewertung."""

    def test_fallback_uses_keyword_matching(self):
        """Fallback verwendet Keyword-Matching."""
        service = OpenAIService(api_key="test-key")

        result = service._create_fallback_evaluation(
            error="Test-Fehler",
            candidate_data={"skills": ["SAP", "DATEV", "Excel"]},
            job_data={"job_text": "Wir suchen jemanden mit SAP und DATEV Kenntnissen."},
        )

        # SAP und DATEV sollten im Job-Text gefunden werden
        assert result.score > 0
        assert "sap" in [s.lower() for s in result.strengths] or "datev" in [s.lower() for s in result.strengths]

    def test_fallback_no_skills_low_score(self):
        """Ohne Skills ist Score niedrig."""
        service = OpenAIService(api_key="test-key")

        result = service._create_fallback_evaluation(
            error="Test-Fehler",
            candidate_data={"skills": []},
            job_data={"job_text": "Wir suchen SAP-Experten."},
        )

        assert result.score == 0.3  # Default-Score ohne Skills

    def test_fallback_error_in_explanation(self):
        """Fehler wird in Erklärung erwähnt."""
        service = OpenAIService(api_key="test-key")

        result = service._create_fallback_evaluation(
            error="API nicht erreichbar",
            candidate_data={"skills": ["Python"]},
            job_data={"job_text": "Python Developer gesucht"},
        )

        assert "API nicht erreichbar" in result.explanation


class TestCostTracking:
    """Tests für Kosten-Tracking."""

    def test_total_usage_accumulates(self):
        """Gesamtverbrauch akkumuliert über mehrere Aufrufe."""
        service = OpenAIService(api_key="test-key")

        usage1 = OpenAIUsage(input_tokens=100, output_tokens=50, total_tokens=150)
        usage2 = OpenAIUsage(input_tokens=200, output_tokens=75, total_tokens=275)

        service._add_usage(usage1)
        service._add_usage(usage2)

        assert service.total_usage.input_tokens == 300
        assert service.total_usage.output_tokens == 125
        assert service.total_usage.total_tokens == 425

    def test_estimate_cost(self):
        """Kostenvoranschlag funktioniert."""
        service = OpenAIService(api_key="test-key")

        # 10 Kandidaten
        estimate = service.estimate_cost(10)

        # 10 * 800 input + 10 * 150 output
        expected_input = (10 * 800 / 1_000_000) * PRICE_INPUT_PER_1M
        expected_output = (10 * 150 / 1_000_000) * PRICE_OUTPUT_PER_1M
        expected = round(expected_input + expected_output, 4)

        assert estimate == expected

    def test_estimate_cost_zero(self):
        """Kostenvoranschlag für 0 Kandidaten ist 0."""
        service = OpenAIService(api_key="test-key")
        assert service.estimate_cost(0) == 0.0


class TestPromptCreation:
    """Tests für Prompt-Erstellung."""

    def test_prompt_includes_job_data(self):
        """Prompt enthält Job-Daten."""
        service = OpenAIService(api_key="test-key")

        prompt = service._create_match_prompt(
            job_data={
                "position": "Buchhalter",
                "company_name": "Test GmbH",
                "industry": "Finanzen",
                "job_text": "Wir suchen SAP-Experten.",
            },
            candidate_data={"skills": []},
        )

        assert "Buchhalter" in prompt
        assert "Test GmbH" in prompt
        assert "Finanzen" in prompt
        assert "SAP-Experten" in prompt

    def test_prompt_includes_candidate_data(self):
        """Prompt enthält Kandidaten-Daten."""
        service = OpenAIService(api_key="test-key")

        prompt = service._create_match_prompt(
            job_data={"position": "Test"},
            candidate_data={
                "full_name": "Max Mustermann",
                "current_position": "Finanzbuchhalter",
                "skills": ["SAP", "DATEV", "Excel"],
            },
        )

        assert "Max Mustermann" in prompt
        assert "Finanzbuchhalter" in prompt
        assert "SAP" in prompt
        assert "DATEV" in prompt

    def test_prompt_truncates_long_job_text(self):
        """Lange Job-Texte werden gekürzt."""
        service = OpenAIService(api_key="test-key")

        long_text = "A" * 5000

        prompt = service._create_match_prompt(
            job_data={"position": "Test", "job_text": long_text},
            candidate_data={"skills": []},
        )

        # Text sollte auf 3000 Zeichen + "..." gekürzt sein
        assert "..." in prompt
        assert len(prompt) < 5000

    def test_prompt_handles_empty_work_history(self):
        """Leere Berufserfahrung wird behandelt."""
        service = OpenAIService(api_key="test-key")

        prompt = service._create_match_prompt(
            job_data={"position": "Test"},
            candidate_data={"skills": [], "work_history": []},
        )

        assert "Keine angegeben" in prompt


class TestContextManager:
    """Tests für Context-Manager."""

    @pytest.mark.asyncio
    async def test_context_manager_closes_client(self):
        """Context-Manager schließt Client."""
        async with OpenAIService(api_key="test-key") as service:
            # Client wurde noch nicht erstellt
            pass

        # Nach Exit sollte close aufgerufen worden sein
        # (Client ist None wenn nie verwendet)


class TestSystemPrompt:
    """Tests für System-Prompt."""

    def test_system_prompt_defined(self):
        """System-Prompt ist definiert."""
        assert MATCHING_SYSTEM_PROMPT is not None
        assert len(MATCHING_SYSTEM_PROMPT) > 100

    def test_system_prompt_german(self):
        """System-Prompt fordert deutsche Antworten."""
        assert "DEUTSCH" in MATCHING_SYSTEM_PROMPT or "Deutsch" in MATCHING_SYSTEM_PROMPT

    def test_system_prompt_json_format(self):
        """System-Prompt erwartet JSON-Format."""
        assert "JSON" in MATCHING_SYSTEM_PROMPT
        assert "score" in MATCHING_SYSTEM_PROMPT
