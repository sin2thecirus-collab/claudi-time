"""Geo-Tests - Distanzberechnung und Radius-Suche mit PostGIS."""

import pytest
from geoalchemy2.functions import ST_Distance, ST_DWithin, ST_MakePoint, ST_SetSRID
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.candidate import Candidate
from app.models.job import Job
from tests.conftest import CandidateFactory, GEO_COORDS, JobFactory

# Marker für Tests die PostgreSQL+PostGIS benötigen
pytestmark = pytest.mark.asyncio


class TestPostGISSetup:
    """Verifiziert dass PostGIS korrekt eingerichtet ist."""

    async def test_postgis_extension_loaded(self, db_session: AsyncSession):
        """PostGIS Extension ist verfügbar."""
        result = await db_session.execute(
            text("SELECT PostGIS_Version()")
        )
        version = result.scalar()
        assert version is not None
        assert "3" in version  # PostGIS 3.x

    async def test_can_create_geography_point(self, db_session: AsyncSession):
        """Geography Point kann erstellt werden."""
        result = await db_session.execute(
            text("SELECT ST_AsText(ST_SetSRID(ST_MakePoint(9.9937, 53.5511), 4326))")
        )
        point_text = result.scalar()
        assert "POINT" in point_text
        assert "9.9937" in point_text


class TestDistanceCalculation:
    """Tests für Distanz-Berechnung mit echten Koordinaten."""

    async def test_distance_hamburg_to_altona(self, db_session: AsyncSession):
        """Distanz Hamburg Zentrum zu Altona (~4km)."""
        lat1, lon1 = GEO_COORDS["hamburg_zentrum"]
        lat2, lon2 = GEO_COORDS["hamburg_altona"]

        result = await db_session.execute(
            text("""
                SELECT ST_Distance(
                    ST_SetSRID(ST_MakePoint(:lon1, :lat1), 4326)::geography,
                    ST_SetSRID(ST_MakePoint(:lon2, :lat2), 4326)::geography
                ) / 1000 AS distance_km
            """),
            {"lat1": lat1, "lon1": lon1, "lat2": lat2, "lon2": lon2},
        )
        distance_km = result.scalar()

        # Altona ist ca. 4km vom Zentrum entfernt
        assert 3.5 <= distance_km <= 5.0

    async def test_distance_hamburg_to_wandsbek(self, db_session: AsyncSession):
        """Distanz Hamburg Zentrum zu Wandsbek (~7km)."""
        lat1, lon1 = GEO_COORDS["hamburg_zentrum"]
        lat2, lon2 = GEO_COORDS["hamburg_wandsbek"]

        result = await db_session.execute(
            text("""
                SELECT ST_Distance(
                    ST_SetSRID(ST_MakePoint(:lon1, :lat1), 4326)::geography,
                    ST_SetSRID(ST_MakePoint(:lon2, :lat2), 4326)::geography
                ) / 1000 AS distance_km
            """),
            {"lat1": lat1, "lon1": lon1, "lat2": lat2, "lon2": lon2},
        )
        distance_km = result.scalar()

        # Wandsbek ist ca. 7km vom Zentrum entfernt
        assert 6.0 <= distance_km <= 9.0

    async def test_distance_hamburg_to_luebeck(self, db_session: AsyncSession):
        """Distanz Hamburg zu Lübeck (~57km) - außerhalb 25km Radius."""
        lat1, lon1 = GEO_COORDS["hamburg_zentrum"]
        lat2, lon2 = GEO_COORDS["luebeck"]

        result = await db_session.execute(
            text("""
                SELECT ST_Distance(
                    ST_SetSRID(ST_MakePoint(:lon1, :lat1), 4326)::geography,
                    ST_SetSRID(ST_MakePoint(:lon2, :lat2), 4326)::geography
                ) / 1000 AS distance_km
            """),
            {"lat1": lat1, "lon1": lon1, "lat2": lat2, "lon2": lon2},
        )
        distance_km = result.scalar()

        # Lübeck ist ca. 57km entfernt - außerhalb des 25km Radius
        assert distance_km > 25
        assert 55 <= distance_km <= 65

    async def test_distance_hamburg_to_berlin(self, db_session: AsyncSession):
        """Distanz Hamburg zu Berlin (~256km)."""
        lat1, lon1 = GEO_COORDS["hamburg_zentrum"]
        lat2, lon2 = GEO_COORDS["berlin"]

        result = await db_session.execute(
            text("""
                SELECT ST_Distance(
                    ST_SetSRID(ST_MakePoint(:lon1, :lat1), 4326)::geography,
                    ST_SetSRID(ST_MakePoint(:lon2, :lat2), 4326)::geography
                ) / 1000 AS distance_km
            """),
            {"lat1": lat1, "lon1": lon1, "lat2": lat2, "lon2": lon2},
        )
        distance_km = result.scalar()

        # Berlin ist ca. 256km entfernt
        assert 250 <= distance_km <= 270


class TestRadiusSearch:
    """Tests für Radius-Suche (≤25km Filter)."""

    async def test_st_dwithin_includes_nearby(self, db_session: AsyncSession):
        """ST_DWithin findet Punkte innerhalb des Radius."""
        lat1, lon1 = GEO_COORDS["hamburg_zentrum"]
        lat2, lon2 = GEO_COORDS["hamburg_altona"]  # ~4km

        result = await db_session.execute(
            text("""
                SELECT ST_DWithin(
                    ST_SetSRID(ST_MakePoint(:lon1, :lat1), 4326)::geography,
                    ST_SetSRID(ST_MakePoint(:lon2, :lat2), 4326)::geography,
                    25000  -- 25km in Metern
                )
            """),
            {"lat1": lat1, "lon1": lon1, "lat2": lat2, "lon2": lon2},
        )
        is_within = result.scalar()
        assert is_within is True

    async def test_st_dwithin_excludes_distant(self, db_session: AsyncSession):
        """ST_DWithin schließt Punkte außerhalb des Radius aus."""
        lat1, lon1 = GEO_COORDS["hamburg_zentrum"]
        lat2, lon2 = GEO_COORDS["luebeck"]  # ~65km

        result = await db_session.execute(
            text("""
                SELECT ST_DWithin(
                    ST_SetSRID(ST_MakePoint(:lon1, :lat1), 4326)::geography,
                    ST_SetSRID(ST_MakePoint(:lon2, :lat2), 4326)::geography,
                    25000  -- 25km in Metern
                )
            """),
            {"lat1": lat1, "lon1": lon1, "lat2": lat2, "lon2": lon2},
        )
        is_within = result.scalar()
        assert is_within is False


class TestCandidateJobGeoMatching:
    """Tests für Geo-Matching zwischen Jobs und Kandidaten."""

    async def test_job_with_location_persisted(self, db_session: AsyncSession):
        """Job mit Koordinaten wird korrekt gespeichert."""
        lat, lon = GEO_COORDS["hamburg_zentrum"]

        job = JobFactory.create(
            city="Hamburg",
            postal_code="20095",
        )
        db_session.add(job)
        await db_session.flush()

        # Location direkt via SQL setzen
        await db_session.execute(
            text("""
                UPDATE jobs SET location_coords = ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)
                WHERE id = :job_id
            """),
            {"lat": lat, "lon": lon, "job_id": str(job.id)},
        )
        await db_session.commit()

        # Verifizieren
        result = await db_session.execute(
            text("SELECT ST_Y(location_coords::geometry), ST_X(location_coords::geometry) FROM jobs WHERE id = :job_id"),
            {"job_id": str(job.id)},
        )
        row = result.one()
        assert abs(row[0] - lat) < 0.0001
        assert abs(row[1] - lon) < 0.0001

    async def test_candidate_with_location_persisted(self, db_session: AsyncSession):
        """Kandidat mit Koordinaten wird korrekt gespeichert."""
        lat, lon = GEO_COORDS["hamburg_altona"]

        candidate = CandidateFactory.create(
            city="Hamburg",
            postal_code="22769",
        )
        db_session.add(candidate)
        await db_session.flush()

        # Location direkt via SQL setzen
        await db_session.execute(
            text("""
                UPDATE candidates SET address_coords = ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)
                WHERE id = :candidate_id
            """),
            {"lat": lat, "lon": lon, "candidate_id": str(candidate.id)},
        )
        await db_session.commit()

        # Verifizieren
        result = await db_session.execute(
            text("SELECT ST_Y(address_coords::geometry), ST_X(address_coords::geometry) FROM candidates WHERE id = :candidate_id"),
            {"candidate_id": str(candidate.id)},
        )
        row = result.one()
        assert abs(row[0] - lat) < 0.0001
        assert abs(row[1] - lon) < 0.0001

    async def test_find_candidates_within_radius(self, db_session: AsyncSession):
        """Findet Kandidaten innerhalb des 25km Radius."""
        job_lat, job_lon = GEO_COORDS["hamburg_zentrum"]

        # Job erstellen
        job = JobFactory.create(city="Hamburg", postal_code="20095")
        db_session.add(job)
        await db_session.flush()
        await db_session.execute(
            text("UPDATE jobs SET location_coords = ST_SetSRID(ST_MakePoint(:lon, :lat), 4326) WHERE id = :id"),
            {"lat": job_lat, "lon": job_lon, "id": str(job.id)},
        )

        # Kandidaten an verschiedenen Orten erstellen
        locations = [
            ("near_altona", GEO_COORDS["hamburg_altona"], True),      # ~4km - sollte gefunden werden
            ("near_wandsbek", GEO_COORDS["hamburg_wandsbek"], True),  # ~7km - sollte gefunden werden
            ("far_luebeck", GEO_COORDS["luebeck"], False),            # ~65km - sollte NICHT gefunden werden
            ("far_berlin", GEO_COORDS["berlin"], False),              # ~290km - sollte NICHT gefunden werden
        ]

        candidate_ids = {}
        for name, (lat, lon), _ in locations:
            candidate = CandidateFactory.create(
                first_name=name,
                crm_id=f"CRM-{name}",
            )
            db_session.add(candidate)
            await db_session.flush()
            candidate_ids[name] = candidate.id
            await db_session.execute(
                text("UPDATE candidates SET address_coords = ST_SetSRID(ST_MakePoint(:lon, :lat), 4326) WHERE id = :id"),
                {"lat": lat, "lon": lon, "id": str(candidate.id)},
            )

        await db_session.commit()

        # Kandidaten im 25km Radius finden
        result = await db_session.execute(
            text("""
                SELECT c.id, c.first_name,
                       ST_Distance(c.address_coords, j.location_coords) / 1000 AS distance_km
                FROM candidates c, jobs j
                WHERE j.id = :job_id
                  AND c.address_coords IS NOT NULL
                  AND ST_DWithin(c.address_coords, j.location_coords, 25000)
                ORDER BY distance_km
            """),
            {"job_id": str(job.id)},
        )
        found_candidates = result.all()

        # Verifizieren: nur die nahen Kandidaten sollten gefunden werden
        found_names = [row[1] for row in found_candidates]
        assert "near_altona" in found_names
        assert "near_wandsbek" in found_names
        assert "far_luebeck" not in found_names
        assert "far_berlin" not in found_names
        assert len(found_candidates) == 2

        # Distanzen prüfen
        for row in found_candidates:
            distance_km = row[2]
            assert distance_km <= 25  # Alle gefundenen müssen ≤25km sein

    async def test_candidates_outside_radius_excluded(self, db_session: AsyncSession):
        """Kandidaten außerhalb des Radius werden korrekt ausgeschlossen."""
        job_lat, job_lon = GEO_COORDS["hamburg_zentrum"]

        # Job in Hamburg
        job = JobFactory.create(city="Hamburg")
        db_session.add(job)
        await db_session.flush()
        await db_session.execute(
            text("UPDATE jobs SET location_coords = ST_SetSRID(ST_MakePoint(:lon, :lat), 4326) WHERE id = :id"),
            {"lat": job_lat, "lon": job_lon, "id": str(job.id)},
        )

        # Kandidat in Bremen (~95km)
        bremen_lat, bremen_lon = GEO_COORDS["bremen"]
        candidate = CandidateFactory.create(city="Bremen", crm_id="CRM-Bremen")
        db_session.add(candidate)
        await db_session.flush()
        await db_session.execute(
            text("UPDATE candidates SET address_coords = ST_SetSRID(ST_MakePoint(:lon, :lat), 4326) WHERE id = :id"),
            {"lat": bremen_lat, "lon": bremen_lon, "id": str(candidate.id)},
        )
        await db_session.commit()

        # Suche im 25km Radius - sollte nichts finden
        result = await db_session.execute(
            text("""
                SELECT COUNT(*) FROM candidates c, jobs j
                WHERE j.id = :job_id
                  AND c.address_coords IS NOT NULL
                  AND ST_DWithin(c.address_coords, j.location_coords, 25000)
            """),
            {"job_id": str(job.id)},
        )
        count = result.scalar()
        assert count == 0

        # Verifizieren dass der Kandidat existiert aber außerhalb ist
        result = await db_session.execute(
            text("""
                SELECT ST_Distance(c.address_coords, j.location_coords) / 1000 AS distance_km
                FROM candidates c, jobs j
                WHERE j.id = :job_id AND c.id = :candidate_id
            """),
            {"job_id": str(job.id), "candidate_id": str(candidate.id)},
        )
        distance = result.scalar()
        assert distance > 25  # Bremen ist >25km von Hamburg entfernt
