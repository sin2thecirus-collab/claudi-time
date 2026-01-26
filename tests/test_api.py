"""Tests für API-Endpoints."""

import uuid

import pytest
from httpx import AsyncClient

from app.models.job import Job
from app.models.candidate import Candidate
from app.models.match import Match


class TestHealthEndpoint:
    """Tests für den Health-Check Endpoint."""

    @pytest.mark.asyncio
    async def test_health_check(self, client: AsyncClient):
        """Health-Check gibt 200 OK zurück."""
        response = await client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"


class TestJobsAPI:
    """Tests für Jobs-API Endpoints."""

    @pytest.mark.asyncio
    async def test_list_jobs_empty(self, client: AsyncClient):
        """Leere Job-Liste wird zurückgegeben."""
        response = await client.get("/api/jobs")
        assert response.status_code == 200
        data = response.json()
        assert "items" in data
        assert len(data["items"]) == 0

    @pytest.mark.asyncio
    async def test_list_jobs_pagination(self, client: AsyncClient):
        """Jobs-Liste unterstützt Pagination."""
        response = await client.get("/api/jobs?page=1&per_page=10")
        assert response.status_code == 200
        data = response.json()
        assert "page" in data
        assert "per_page" in data
        assert "total" in data

    @pytest.mark.asyncio
    async def test_get_job_not_found(self, client: AsyncClient):
        """Nicht existierender Job gibt 404 zurück."""
        fake_id = uuid.uuid4()
        response = await client.get(f"/api/jobs/{fake_id}")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_get_job_invalid_uuid(self, client: AsyncClient):
        """Ungültige UUID gibt 422 zurück."""
        response = await client.get("/api/jobs/invalid-uuid")
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_delete_job_not_found(self, client: AsyncClient):
        """Löschen nicht existierendem Job gibt 404 zurück."""
        fake_id = uuid.uuid4()
        response = await client.delete(f"/api/jobs/{fake_id}")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_batch_delete_empty_list(self, client: AsyncClient):
        """Batch-Delete mit leerer Liste gibt 422 zurück."""
        response = await client.request(
            "DELETE",
            "/api/jobs/batch",
            json={"ids": []},
        )
        assert response.status_code == 422


class TestCandidatesAPI:
    """Tests für Candidates-API Endpoints."""

    @pytest.mark.asyncio
    async def test_list_candidates_empty(self, client: AsyncClient):
        """Leere Kandidaten-Liste wird zurückgegeben."""
        response = await client.get("/api/candidates")
        assert response.status_code == 200
        data = response.json()
        assert "items" in data
        assert len(data["items"]) == 0

    @pytest.mark.asyncio
    async def test_list_candidates_pagination(self, client: AsyncClient):
        """Kandidaten-Liste unterstützt Pagination."""
        response = await client.get("/api/candidates?page=1&per_page=10")
        assert response.status_code == 200
        data = response.json()
        assert "page" in data
        assert "per_page" in data
        assert "total" in data

    @pytest.mark.asyncio
    async def test_get_candidate_not_found(self, client: AsyncClient):
        """Nicht existierender Kandidat gibt 404 zurück."""
        fake_id = uuid.uuid4()
        response = await client.get(f"/api/candidates/{fake_id}")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_hide_candidate_not_found(self, client: AsyncClient):
        """Ausblenden nicht existierendem Kandidat gibt 404 zurück."""
        fake_id = uuid.uuid4()
        response = await client.put(f"/api/candidates/{fake_id}/hide")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_unhide_candidate_not_found(self, client: AsyncClient):
        """Einblenden nicht existierendem Kandidat gibt 404 zurück."""
        fake_id = uuid.uuid4()
        response = await client.put(f"/api/candidates/{fake_id}/unhide")
        assert response.status_code == 404


class TestMatchesAPI:
    """Tests für Matches-API Endpoints."""

    @pytest.mark.asyncio
    async def test_get_matches_for_job_not_found(self, client: AsyncClient):
        """Matches für nicht existierenden Job gibt 404 zurück."""
        fake_id = uuid.uuid4()
        response = await client.get(f"/api/matches/job/{fake_id}")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_get_match_not_found(self, client: AsyncClient):
        """Nicht existierendes Match gibt 404 zurück."""
        fake_id = uuid.uuid4()
        response = await client.get(f"/api/matches/{fake_id}")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_ai_check_empty_candidates(self, client: AsyncClient):
        """AI-Check ohne Kandidaten gibt Fehler zurück."""
        fake_job_id = uuid.uuid4()
        response = await client.post(
            "/api/matches/ai-check",
            json={
                "job_id": str(fake_job_id),
                "candidate_ids": [],
            },
        )
        # Entweder 422 (Validierungsfehler) oder 404 (Job nicht gefunden)
        assert response.status_code in [404, 422]

    @pytest.mark.asyncio
    async def test_ai_check_over_limit(self, client: AsyncClient):
        """AI-Check mit zu vielen Kandidaten gibt 422 zurück."""
        from app.config import Limits

        fake_job_id = uuid.uuid4()
        candidate_ids = [str(uuid.uuid4()) for _ in range(Limits.AI_CHECK_MAX_CANDIDATES + 1)]

        response = await client.post(
            "/api/matches/ai-check",
            json={
                "job_id": str(fake_job_id),
                "candidate_ids": candidate_ids,
            },
        )
        assert response.status_code == 422


class TestFiltersAPI:
    """Tests für Filters-API Endpoints."""

    @pytest.mark.asyncio
    async def test_get_filter_options(self, client: AsyncClient):
        """Filter-Optionen werden zurückgegeben."""
        response = await client.get("/api/filters/options")
        assert response.status_code == 200
        data = response.json()
        assert "cities" in data
        assert "skills" in data
        assert "industries" in data

    @pytest.mark.asyncio
    async def test_get_cities(self, client: AsyncClient):
        """Städte-Liste wird zurückgegeben."""
        response = await client.get("/api/filters/cities")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_get_skills(self, client: AsyncClient):
        """Skills-Liste wird zurückgegeben."""
        response = await client.get("/api/filters/skills")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_get_industries(self, client: AsyncClient):
        """Branchen-Liste wird zurückgegeben."""
        response = await client.get("/api/filters/industries")
        assert response.status_code == 200


class TestSettingsAPI:
    """Tests für Settings-API Endpoints."""

    @pytest.mark.asyncio
    async def test_get_priority_cities(self, client: AsyncClient):
        """Prio-Städte werden zurückgegeben."""
        response = await client.get("/api/settings/priority-cities")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_get_limits(self, client: AsyncClient):
        """System-Limits werden zurückgegeben."""
        response = await client.get("/api/settings/limits")
        assert response.status_code == 200
        data = response.json()
        assert "csv_max_file_size_mb" in data
        assert "batch_delete_max" in data
        assert "ai_check_max_candidates" in data


class TestAlertsAPI:
    """Tests für Alerts-API Endpoints."""

    @pytest.mark.asyncio
    async def test_get_alerts(self, client: AsyncClient):
        """Alerts-Liste wird zurückgegeben."""
        response = await client.get("/api/alerts")
        assert response.status_code == 200
        data = response.json()
        assert "items" in data

    @pytest.mark.asyncio
    async def test_get_active_alerts(self, client: AsyncClient):
        """Aktive Alerts werden zurückgegeben."""
        response = await client.get("/api/alerts/active")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_get_alert_not_found(self, client: AsyncClient):
        """Nicht existierender Alert gibt 404 zurück."""
        fake_id = uuid.uuid4()
        response = await client.get(f"/api/alerts/{fake_id}")
        assert response.status_code == 404


class TestStatisticsAPI:
    """Tests für Statistics-API Endpoints."""

    @pytest.mark.asyncio
    async def test_get_dashboard_stats(self, client: AsyncClient):
        """Dashboard-Statistiken werden zurückgegeben."""
        response = await client.get("/api/statistics/dashboard")
        assert response.status_code == 200
        data = response.json()
        assert "jobs_active" in data
        assert "candidates_total" in data

    @pytest.mark.asyncio
    async def test_get_jobs_without_matches(self, client: AsyncClient):
        """Jobs ohne Matches werden zurückgegeben."""
        response = await client.get("/api/statistics/jobs-without-matches")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_get_candidates_without_address(self, client: AsyncClient):
        """Kandidaten ohne Adresse werden zurückgegeben."""
        response = await client.get("/api/statistics/candidates-without-address")
        assert response.status_code == 200


class TestAdminAPI:
    """Tests für Admin-API Endpoints."""

    @pytest.mark.asyncio
    async def test_get_geocoding_status(self, client: AsyncClient):
        """Geocoding-Status wird zurückgegeben."""
        response = await client.get("/api/admin/geocoding/status")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_get_crm_sync_status(self, client: AsyncClient):
        """CRM-Sync-Status wird zurückgegeben."""
        response = await client.get("/api/admin/crm-sync/status")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_get_matching_status(self, client: AsyncClient):
        """Matching-Status wird zurückgegeben."""
        response = await client.get("/api/admin/matching/status")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_get_admin_status(self, client: AsyncClient):
        """Admin-Übersicht wird zurückgegeben."""
        response = await client.get("/api/admin/status")
        assert response.status_code == 200


class TestErrorHandling:
    """Tests für Error-Handling."""

    @pytest.mark.asyncio
    async def test_invalid_json_body(self, client: AsyncClient):
        """Ungültiger JSON-Body gibt 422 zurück."""
        response = await client.post(
            "/api/matches/ai-check",
            content="invalid json",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_missing_required_field(self, client: AsyncClient):
        """Fehlende Pflichtfelder geben 422 zurück."""
        response = await client.post(
            "/api/matches/ai-check",
            json={
                # job_id fehlt
                "candidate_ids": [],
            },
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_not_found_returns_json(self, client: AsyncClient):
        """404-Fehler geben JSON zurück."""
        fake_id = uuid.uuid4()
        response = await client.get(f"/api/jobs/{fake_id}")
        assert response.status_code == 404
        data = response.json()
        assert "detail" in data or "error" in data
