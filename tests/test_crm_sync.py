"""Tests für CRM Sync Service mit Mocks."""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.crm_client import (
    CRMError,
    CRMNotFoundError,
    CRMRateLimitError,
    RecruitCRMClient,
)
from app.services.crm_sync_service import CRMSyncService, SyncResult


class TestSyncResult:
    """Tests für SyncResult Datenklasse."""

    def test_success_rate_full(self):
        """Erfolgsrate bei vollem Erfolg ist 100%."""
        result = SyncResult(
            total_processed=10,
            created=5,
            updated=3,
            skipped=2,
            failed=0,
        )
        assert result.success_rate == 100.0

    def test_success_rate_partial(self):
        """Erfolgsrate bei teilweisem Erfolg."""
        result = SyncResult(
            total_processed=10,
            created=4,
            updated=3,
            skipped=1,
            failed=2,
        )
        # (4+3+1) / 10 = 80%
        assert result.success_rate == 80.0

    def test_success_rate_zero_processed(self):
        """Erfolgsrate bei 0 verarbeiteten ist 100%."""
        result = SyncResult(total_processed=0)
        assert result.success_rate == 100.0

    def test_new_candidate_ids_tracked(self):
        """Neue Kandidaten-IDs werden getrackt."""
        result = SyncResult()
        new_id = uuid.uuid4()
        result.new_candidate_ids.append(new_id)
        assert len(result.new_candidate_ids) == 1
        assert result.new_candidate_ids[0] == new_id


class TestCRMExceptions:
    """Tests für CRM Exceptions."""

    def test_crm_error_message(self):
        """CRMError hat Nachricht und Status-Code."""
        error = CRMError("Test-Fehler", status_code=500)
        assert error.message == "Test-Fehler"
        assert error.status_code == 500
        assert "Test-Fehler" in str(error)

    def test_rate_limit_error(self):
        """RateLimitError hat retry_after."""
        error = CRMRateLimitError(retry_after=120)
        assert error.retry_after == 120
        assert error.status_code == 429

    def test_rate_limit_error_default(self):
        """RateLimitError hat Standard retry_after."""
        error = CRMRateLimitError()
        assert error.retry_after == 60

    def test_not_found_error(self):
        """NotFoundError enthält Ressource."""
        error = CRMNotFoundError("candidate-123")
        assert "candidate-123" in error.message
        assert error.status_code == 404


class TestRecruitCRMClient:
    """Tests für RecruitCRMClient."""

    def test_client_requires_api_key(self):
        """Client erfordert API-Key."""
        with patch("app.services.crm_client.settings") as mock_settings:
            mock_settings.recruit_crm_api_key = None
            mock_settings.recruit_crm_base_url = "https://api.test.com"
            with pytest.raises(ValueError, match="API-Key"):
                RecruitCRMClient()

    def test_client_with_api_key(self):
        """Client wird mit API-Key initialisiert."""
        client = RecruitCRMClient(
            api_key="test-key",
            base_url="https://api.test.com",
        )
        assert client.api_key == "test-key"
        assert "api.test.com" in client.base_url

    def test_client_strips_trailing_slash(self):
        """Trailing Slash wird entfernt."""
        client = RecruitCRMClient(
            api_key="test-key",
            base_url="https://api.test.com/",
        )
        assert not client.base_url.endswith("/")


class TestCRMSyncService:
    """Tests für CRMSyncService."""

    @pytest.fixture
    def mock_db_session(self):
        """Erstellt eine Mock-DB-Session."""
        session = AsyncMock()
        session.execute = AsyncMock()
        session.commit = AsyncMock()
        session.flush = AsyncMock()
        session.add = MagicMock()
        return session

    @pytest.fixture
    def mock_crm_client(self):
        """Erstellt einen Mock-CRM-Client."""
        client = AsyncMock(spec=RecruitCRMClient)
        client.close = AsyncMock()
        return client

    @pytest.mark.asyncio
    async def test_has_candidates_true(self, mock_db_session):
        """has_candidates gibt True wenn Kandidaten existieren."""
        mock_result = MagicMock()
        mock_result.scalar_one.return_value = 5
        mock_db_session.execute.return_value = mock_result

        service = CRMSyncService(mock_db_session)
        result = await service.has_candidates()

        assert result is True

    @pytest.mark.asyncio
    async def test_has_candidates_false(self, mock_db_session):
        """has_candidates gibt False wenn keine Kandidaten."""
        mock_result = MagicMock()
        mock_result.scalar_one.return_value = 0
        mock_db_session.execute.return_value = mock_result

        service = CRMSyncService(mock_db_session)
        result = await service.has_candidates()

        assert result is False

    @pytest.mark.asyncio
    async def test_sync_all_initial_when_empty(self, mock_db_session, mock_crm_client):
        """sync_all startet Initial-Sync wenn DB leer."""
        # has_candidates -> False
        mock_result_count = MagicMock()
        mock_result_count.scalar_one.return_value = 0
        mock_db_session.execute.return_value = mock_result_count

        # Mock get_all_candidates_paginated als async generator
        async def mock_paginated(*args, **kwargs):
            yield 1, [], 0  # Leere Seite

        mock_crm_client.get_all_candidates_paginated = mock_paginated

        service = CRMSyncService(mock_db_session, mock_crm_client)

        with patch.object(service, "initial_sync", new_callable=AsyncMock) as mock_initial:
            mock_initial.return_value = SyncResult(is_initial_sync=True)
            result = await service.sync_all()

            mock_initial.assert_called_once()
            assert result.is_initial_sync is True

    @pytest.mark.asyncio
    async def test_initial_sync_creates_candidates(self, mock_db_session, mock_crm_client):
        """Initial-Sync erstellt neue Kandidaten."""
        # Mock CRM-Daten
        crm_candidates = [
            {"id": "crm-1", "first_name": "Max", "last_name": "Mustermann", "email": "max@test.de"},
            {"id": "crm-2", "first_name": "Anna", "last_name": "Schmidt", "email": "anna@test.de"},
        ]

        # Mock paginated response
        async def mock_paginated(*args, **kwargs):
            yield 1, crm_candidates, 2

        mock_crm_client.get_all_candidates_paginated = mock_paginated
        mock_crm_client.map_to_candidate_data = lambda d: {
            "crm_id": d["id"],
            "first_name": d["first_name"],
            "last_name": d["last_name"],
            "email": d["email"],
        }

        # Mock: Kandidat existiert nicht
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db_session.execute.return_value = mock_result

        service = CRMSyncService(mock_db_session, mock_crm_client)
        result = await service.initial_sync()

        assert result.is_initial_sync is True
        assert result.created == 2
        assert result.total_processed == 2
        assert len(result.new_candidate_ids) == 2

    @pytest.mark.asyncio
    async def test_initial_sync_updates_existing(self, mock_db_session, mock_crm_client):
        """Initial-Sync aktualisiert existierende Kandidaten."""
        crm_candidates = [
            {"id": "crm-1", "first_name": "Max", "last_name": "Neu", "email": "max@test.de"},
        ]

        async def mock_paginated(*args, **kwargs):
            yield 1, crm_candidates, 1

        mock_crm_client.get_all_candidates_paginated = mock_paginated
        mock_crm_client.map_to_candidate_data = lambda d: {
            "crm_id": d["id"],
            "first_name": d["first_name"],
            "last_name": d["last_name"],
        }

        # Mock: Kandidat existiert bereits
        existing_candidate = MagicMock()
        existing_candidate.id = uuid.uuid4()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing_candidate
        mock_db_session.execute.return_value = mock_result

        service = CRMSyncService(mock_db_session, mock_crm_client)
        result = await service.initial_sync()

        assert result.updated == 1
        assert result.created == 0

    @pytest.mark.asyncio
    async def test_incremental_sync_delta_only(self, mock_db_session, mock_crm_client):
        """Incremental-Sync verarbeitet nur Änderungen."""
        since = datetime(2026, 1, 1, tzinfo=timezone.utc)

        # Nur 1 geänderter Kandidat
        crm_candidates = [
            {"id": "crm-changed", "first_name": "Geändert", "email": "changed@test.de"},
        ]

        async def mock_paginated(*args, **kwargs):
            yield 1, crm_candidates, 1

        mock_crm_client.get_all_candidates_paginated = mock_paginated
        mock_crm_client.map_to_candidate_data = lambda d: {
            "crm_id": d["id"],
            "first_name": d["first_name"],
        }

        # Kandidat existiert nicht
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db_session.execute.return_value = mock_result

        service = CRMSyncService(mock_db_session, mock_crm_client)
        result = await service.incremental_sync(since=since)

        assert result.is_initial_sync is False
        assert result.total_processed == 1

    @pytest.mark.asyncio
    async def test_sync_handles_rate_limit(self, mock_db_session, mock_crm_client):
        """Sync behandelt Rate-Limit-Fehler."""
        async def mock_paginated(*args, **kwargs):
            raise CRMRateLimitError(retry_after=60)
            yield  # Macht es zum Generator

        mock_crm_client.get_all_candidates_paginated = mock_paginated

        service = CRMSyncService(mock_db_session, mock_crm_client)
        result = await service.initial_sync()

        assert len(result.errors) > 0
        assert any("rate_limit" in str(e.get("type", "")) for e in result.errors)

    @pytest.mark.asyncio
    async def test_sync_handles_crm_error(self, mock_db_session, mock_crm_client):
        """Sync behandelt CRM-Fehler."""
        async def mock_paginated(*args, **kwargs):
            raise CRMError("API nicht erreichbar", status_code=500)
            yield

        mock_crm_client.get_all_candidates_paginated = mock_paginated

        service = CRMSyncService(mock_db_session, mock_crm_client)
        result = await service.initial_sync()

        assert len(result.errors) > 0
        assert any("crm_error" in str(e.get("type", "")) for e in result.errors)

    @pytest.mark.asyncio
    async def test_sync_single_candidate(self, mock_db_session, mock_crm_client):
        """Einzelner Kandidat kann synchronisiert werden."""
        crm_data = {
            "id": "crm-single",
            "first_name": "Single",
            "last_name": "Test",
            "email": "single@test.de",
        }

        mock_crm_client.get_candidate = AsyncMock(return_value=crm_data)
        mock_crm_client.map_to_candidate_data = lambda d: {
            "crm_id": d["id"],
            "first_name": d["first_name"],
            "last_name": d["last_name"],
        }

        # Kandidat existiert nicht
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db_session.execute.return_value = mock_result

        service = CRMSyncService(mock_db_session, mock_crm_client)
        candidate = await service.sync_single_candidate("crm-single")

        assert candidate is not None
        mock_crm_client.get_candidate.assert_called_once_with("crm-single")

    @pytest.mark.asyncio
    async def test_upsert_requires_crm_id(self, mock_db_session, mock_crm_client):
        """Upsert erfordert CRM-ID."""
        mock_crm_client.map_to_candidate_data = lambda d: {"first_name": "Test"}  # Keine crm_id

        service = CRMSyncService(mock_db_session, mock_crm_client)

        with pytest.raises(ValueError, match="CRM-ID"):
            await service._upsert_candidate({"id": None})


class TestCRMSyncServiceContextManager:
    """Tests für Context-Manager."""

    @pytest.mark.asyncio
    async def test_context_manager_closes(self):
        """Context-Manager schließt Ressourcen."""
        mock_db = AsyncMock()
        mock_client = AsyncMock(spec=RecruitCRMClient)
        mock_client.close = AsyncMock()

        # Service mit eigenem Client (owns_client=False)
        async with CRMSyncService(mock_db, mock_client):
            pass

        # Client wurde nicht geschlossen (Service besitzt ihn nicht)
        mock_client.close.assert_not_called()


class TestDeltaSync:
    """Tests für Delta-Sync Logik."""

    @pytest.fixture
    def mock_db_session(self):
        session = AsyncMock()
        session.execute = AsyncMock()
        session.commit = AsyncMock()
        return session

    @pytest.mark.asyncio
    async def test_incremental_without_since_uses_last_sync(self, mock_db_session):
        """Incremental ohne since verwendet letzten Sync-Zeitpunkt."""
        # Mock get_last_sync_time
        mock_result_job = MagicMock()
        mock_result_job.scalar_one_or_none.return_value = None
        mock_result_time = MagicMock()
        mock_result_time.scalar_one_or_none.return_value = datetime(2026, 1, 1, tzinfo=timezone.utc)

        call_count = 0

        def mock_execute(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return mock_result_job if call_count == 1 else mock_result_time
            # Für weitere Aufrufe (Kandidaten-Check)
            return mock_result_job

        mock_db_session.execute = AsyncMock(side_effect=mock_execute)

        mock_client = AsyncMock(spec=RecruitCRMClient)

        async def mock_paginated(*args, **kwargs):
            yield 1, [], 0  # Keine Änderungen

        mock_client.get_all_candidates_paginated = mock_paginated

        service = CRMSyncService(mock_db_session, mock_client)
        result = await service.incremental_sync()

        assert result.is_initial_sync is False

    @pytest.mark.asyncio
    async def test_incremental_falls_back_to_initial(self, mock_db_session):
        """Incremental fällt auf Initial zurück ohne Sync-Zeitpunkt."""
        # Kein letzter Sync-Zeitpunkt
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db_session.execute.return_value = mock_result

        mock_client = AsyncMock(spec=RecruitCRMClient)

        async def mock_paginated(*args, **kwargs):
            yield 1, [], 0

        mock_client.get_all_candidates_paginated = mock_paginated

        service = CRMSyncService(mock_db_session, mock_client)

        # Mock initial_sync
        with patch.object(service, "initial_sync", new_callable=AsyncMock) as mock_initial:
            mock_initial.return_value = SyncResult(is_initial_sync=True)
            result = await service.incremental_sync()

            mock_initial.assert_called_once()
            assert result.is_initial_sync is True
