"""Alert Service - Verwaltet System-Benachrichtigungen.

Dieser Service bietet:
- Automatische Alert-Erstellung für exzellente Matches
- Alert-Erstellung für ablaufende Jobs
- Alert-Management (lesen, verwerfen)
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alert import Alert, AlertPriority, AlertType
from app.models.candidate import Candidate
from app.models.job import Job
from app.models.match import Match, MatchStatus

logger = logging.getLogger(__name__)


class AlertService:
    """Service für System-Benachrichtigungen."""

    def __init__(self, db: AsyncSession):
        """Initialisiert den Service.

        Args:
            db: Async Database Session
        """
        self.db = db

    # ==================== Alert-Abfragen ====================

    async def get_active_alerts(self, limit: int = 10) -> list[Alert]:
        """Gibt aktive (ungelesene, nicht verworfene) Alerts zurück.

        Args:
            limit: Maximale Anzahl

        Returns:
            Liste von aktiven Alerts, sortiert nach Priorität und Datum
        """
        query = (
            select(Alert)
            .where(
                and_(
                    Alert.is_read.is_(False),
                    Alert.is_dismissed.is_(False),
                )
            )
            .order_by(
                # HIGH > MEDIUM > LOW
                Alert.priority.desc(),
                Alert.created_at.desc(),
            )
            .limit(limit)
        )

        result = await self.db.execute(query)
        return list(result.scalars().all())

    async def get_all_alerts(
        self,
        include_dismissed: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Alert], int]:
        """Gibt alle Alerts zurück (optional inkl. verworfene).

        Args:
            include_dismissed: Auch verworfene Alerts einbeziehen
            limit: Maximale Anzahl
            offset: Offset für Pagination

        Returns:
            Tuple (Liste von Alerts, Gesamtanzahl)
        """
        # Basisfilter
        filters = []
        if not include_dismissed:
            filters.append(Alert.is_dismissed.is_(False))

        # Zählung
        count_query = select(func.count(Alert.id))
        if filters:
            count_query = count_query.where(and_(*filters))
        count_result = await self.db.execute(count_query)
        total = count_result.scalar() or 0

        # Alerts laden
        query = (
            select(Alert)
            .order_by(Alert.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        if filters:
            query = query.where(and_(*filters))

        result = await self.db.execute(query)
        alerts = list(result.scalars().all())

        return alerts, total

    async def get_alert(self, alert_id: UUID) -> Alert | None:
        """Gibt einen Alert zurück.

        Args:
            alert_id: Alert-ID

        Returns:
            Alert oder None
        """
        return await self.db.get(Alert, alert_id)

    # ==================== Alert-Aktionen ====================

    async def mark_as_read(self, alert_id: UUID) -> Alert | None:
        """Markiert einen Alert als gelesen.

        Args:
            alert_id: Alert-ID

        Returns:
            Aktualisierter Alert oder None
        """
        alert = await self.get_alert(alert_id)
        if alert:
            alert.is_read = True
            await self.db.commit()
            await self.db.refresh(alert)
        return alert

    async def dismiss(self, alert_id: UUID) -> Alert | None:
        """Verwirft einen Alert (wird nicht mehr angezeigt).

        Args:
            alert_id: Alert-ID

        Returns:
            Aktualisierter Alert oder None
        """
        alert = await self.get_alert(alert_id)
        if alert:
            alert.is_dismissed = True
            await self.db.commit()
            await self.db.refresh(alert)
        return alert

    async def dismiss_all(self) -> int:
        """Verwirft alle aktiven Alerts.

        Returns:
            Anzahl der verworfenen Alerts
        """
        query = (
            update(Alert)
            .where(
                and_(
                    Alert.is_dismissed.is_(False),
                )
            )
            .values(is_dismissed=True)
        )
        result = await self.db.execute(query)
        await self.db.commit()
        return result.rowcount

    async def mark_all_as_read(self) -> int:
        """Markiert alle Alerts als gelesen.

        Returns:
            Anzahl der markierten Alerts
        """
        query = (
            update(Alert)
            .where(
                and_(
                    Alert.is_read.is_(False),
                    Alert.is_dismissed.is_(False),
                )
            )
            .values(is_read=True)
        )
        result = await self.db.execute(query)
        await self.db.commit()
        return result.rowcount

    # ==================== Alert-Erstellung ====================

    async def create_alert(
        self,
        alert_type: AlertType,
        title: str,
        message: str,
        priority: AlertPriority = AlertPriority.MEDIUM,
        job_id: UUID | None = None,
        candidate_id: UUID | None = None,
        match_id: UUID | None = None,
    ) -> Alert:
        """Erstellt einen neuen Alert.

        Args:
            alert_type: Typ des Alerts
            title: Titel
            message: Nachricht
            priority: Priorität (Standard: MEDIUM)
            job_id: Optional - verknüpfter Job
            candidate_id: Optional - verknüpfter Kandidat
            match_id: Optional - verknüpftes Match

        Returns:
            Erstellter Alert
        """
        alert = Alert(
            alert_type=alert_type,
            title=title,
            message=message,
            priority=priority,
            job_id=job_id,
            candidate_id=candidate_id,
            match_id=match_id,
        )
        self.db.add(alert)
        await self.db.commit()
        await self.db.refresh(alert)

        logger.info(f"Alert erstellt: {alert_type.value} - {title}")
        return alert

    # ==================== Automatische Alerts ====================

    async def check_for_excellent_matches(self) -> int:
        """Prüft auf exzellente Matches und erstellt Alerts.

        Exzellente Matches:
        - Distanz ≤ 5km
        - Mindestens 3 gematchte Keywords
        - Status: NEW (noch nicht bearbeitet)
        - Noch kein Alert vorhanden

        Returns:
            Anzahl der erstellten Alerts
        """
        # Subquery für bereits existierende Alerts
        existing_alerts = (
            select(Alert.match_id)
            .where(
                and_(
                    Alert.alert_type == AlertType.EXCELLENT_MATCH,
                    Alert.match_id.is_not(None),
                )
            )
            .distinct()
        )

        # Exzellente Matches ohne Alert finden
        query = (
            select(Match)
            .options()
            .where(
                and_(
                    Match.distance_km.is_not(None),
                    Match.distance_km <= 5,
                    Match.matched_keywords.is_not(None),
                    func.array_length(Match.matched_keywords, 1) >= 3,
                    Match.status == MatchStatus.NEW,
                    Match.id.not_in(existing_alerts),
                )
            )
            .limit(20)  # Max. 20 Alerts auf einmal
        )

        result = await self.db.execute(query)
        matches = result.scalars().all()

        created_count = 0
        for match in matches:
            # Job und Kandidat laden für Alert-Text
            job = await self.db.get(Job, match.job_id)
            candidate = await self.db.get(Candidate, match.candidate_id)

            if not job or not candidate:
                continue

            keywords_str = ", ".join(match.matched_keywords[:5])

            await self.create_alert(
                alert_type=AlertType.EXCELLENT_MATCH,
                title=f"Exzellenter Match gefunden!",
                message=(
                    f"{candidate.full_name} passt hervorragend zu "
                    f"{job.position} bei {job.company_name}. "
                    f"Distanz: {match.distance_km:.1f} km, "
                    f"Keywords: {keywords_str}"
                ),
                priority=AlertPriority.HIGH,
                job_id=match.job_id,
                candidate_id=match.candidate_id,
                match_id=match.id,
            )
            created_count += 1

        if created_count > 0:
            logger.info(f"{created_count} Alerts für exzellente Matches erstellt")

        return created_count

    async def check_for_expiring_jobs(self, days: int = 7) -> int:
        """Prüft auf ablaufende Jobs und erstellt Alerts.

        Args:
            days: Tage bis zum Ablauf (Standard: 7)

        Returns:
            Anzahl der erstellten Alerts
        """
        now = datetime.now(timezone.utc)
        expiry_threshold = now + timedelta(days=days)

        # Subquery für bereits existierende Alerts
        existing_alerts = (
            select(Alert.job_id)
            .where(
                and_(
                    Alert.alert_type == AlertType.EXPIRING_JOB,
                    Alert.job_id.is_not(None),
                    Alert.created_at > now - timedelta(days=days),  # Kein Duplikat
                )
            )
            .distinct()
        )

        # Ablaufende Jobs mit Matches finden
        query = (
            select(Job)
            .where(
                and_(
                    Job.deleted_at.is_(None),
                    Job.expires_at.is_not(None),
                    Job.expires_at <= expiry_threshold,
                    Job.expires_at > now,
                    Job.id.not_in(existing_alerts),
                )
            )
            .limit(20)
        )

        result = await self.db.execute(query)
        jobs = result.scalars().all()

        created_count = 0
        for job in jobs:
            # Prüfen ob der Job Matches hat
            match_count_query = select(func.count(Match.id)).where(
                Match.job_id == job.id
            )
            match_count_result = await self.db.execute(match_count_query)
            match_count = match_count_result.scalar() or 0

            if match_count == 0:
                continue  # Kein Alert für Jobs ohne Matches

            days_until_expiry = (job.expires_at - now).days

            await self.create_alert(
                alert_type=AlertType.EXPIRING_JOB,
                title=f"Job läuft bald ab",
                message=(
                    f"Der Job '{job.position}' bei {job.company_name} "
                    f"läuft in {days_until_expiry} Tagen ab. "
                    f"Es gibt {match_count} potenzielle Kandidaten."
                ),
                priority=AlertPriority.MEDIUM,
                job_id=job.id,
            )
            created_count += 1

        if created_count > 0:
            logger.info(f"{created_count} Alerts für ablaufende Jobs erstellt")

        return created_count

    async def create_sync_error_alert(self, error_message: str) -> Alert:
        """Erstellt einen Alert für Sync-Fehler.

        Args:
            error_message: Fehlermeldung

        Returns:
            Erstellter Alert
        """
        return await self.create_alert(
            alert_type=AlertType.SYNC_ERROR,
            title="CRM-Sync fehlgeschlagen",
            message=f"Der CRM-Sync ist fehlgeschlagen: {error_message}",
            priority=AlertPriority.HIGH,
        )

    async def create_import_complete_alert(
        self,
        filename: str,
        total_rows: int,
        successful: int,
        failed: int,
    ) -> Alert:
        """Erstellt einen Alert für abgeschlossenen Import.

        Args:
            filename: Name der importierten Datei
            total_rows: Gesamtanzahl der Zeilen
            successful: Erfolgreich importiert
            failed: Fehlgeschlagen

        Returns:
            Erstellter Alert
        """
        if failed > 0:
            priority = AlertPriority.MEDIUM
            message = (
                f"Import von '{filename}' abgeschlossen: "
                f"{successful} von {total_rows} Jobs importiert, "
                f"{failed} fehlgeschlagen."
            )
        else:
            priority = AlertPriority.LOW
            message = (
                f"Import von '{filename}' erfolgreich abgeschlossen: "
                f"{successful} Jobs importiert."
            )

        return await self.create_alert(
            alert_type=AlertType.IMPORT_COMPLETE,
            title="Import abgeschlossen",
            message=message,
            priority=priority,
        )

    # ==================== Alert-Cleanup ====================

    async def cleanup_old_alerts(self, days: int = 30) -> int:
        """Löscht alte, verworfene Alerts.

        Args:
            days: Alter in Tagen (Standard: 30)

        Returns:
            Anzahl der gelöschten Alerts
        """
        threshold = datetime.now(timezone.utc) - timedelta(days=days)

        query = (
            select(Alert)
            .where(
                and_(
                    Alert.is_dismissed.is_(True),
                    Alert.created_at < threshold,
                )
            )
        )

        result = await self.db.execute(query)
        alerts = result.scalars().all()

        for alert in alerts:
            await self.db.delete(alert)

        await self.db.commit()

        if alerts:
            logger.info(f"{len(alerts)} alte Alerts gelöscht")

        return len(alerts)

    # ==================== Cron-Job Hilfsfunktion ====================

    async def run_all_checks(self) -> dict[str, int]:
        """Führt alle automatischen Alert-Checks aus.

        Wird vom nächtlichen Cron-Job aufgerufen.

        Returns:
            Dict mit Anzahl der erstellten Alerts pro Typ
        """
        excellent_matches = await self.check_for_excellent_matches()
        expiring_jobs = await self.check_for_expiring_jobs()
        cleaned_up = await self.cleanup_old_alerts()

        return {
            "excellent_matches": excellent_matches,
            "expiring_jobs": expiring_jobs,
            "cleaned_up": cleaned_up,
        }
