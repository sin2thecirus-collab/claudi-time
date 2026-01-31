"""Cloudflare R2 Object Storage Service fuer CV-Dateien."""

import logging
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.config import settings

logger = logging.getLogger(__name__)


def _get_s3_client():
    """Erstellt einen S3-kompatiblen Client fuer Cloudflare R2."""
    if not settings.r2_access_key_id or not settings.r2_endpoint_url:
        logger.warning("R2 nicht konfiguriert - Storage deaktiviert")
        return None

    return boto3.client(
        "s3",
        endpoint_url=settings.r2_endpoint_url,
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
        config=Config(
            signature_version="s3v4",
            retries={"max_attempts": 3, "mode": "adaptive"},
        ),
        region_name="auto",
    )


class R2StorageService:
    """Service fuer Upload/Download von CVs in Cloudflare R2."""

    def __init__(self):
        self.client = _get_s3_client()
        self.bucket = settings.r2_bucket_name

    @property
    def is_available(self) -> bool:
        """Prueft ob R2 konfiguriert und verfuegbar ist."""
        return self.client is not None

    def _build_key(self, candidate_id: str, filename: str | None = None) -> str:
        """
        Baut den R2-Object-Key (Pfad) fuer einen CV.

        Format: cvs/{candidate_uuid}.pdf
        """
        ext = "pdf"
        if filename and "." in filename:
            ext = filename.rsplit(".", 1)[-1].lower()
        return f"cvs/{candidate_id}.{ext}"

    def upload_cv(self, candidate_id: str, file_content: bytes, filename: str | None = None) -> str:
        """
        Laedt einen CV nach R2 hoch.

        Args:
            candidate_id: UUID des Kandidaten
            file_content: PDF-Bytes
            filename: Optionaler Dateiname (fuer Extension)

        Returns:
            R2 Object Key (Pfad im Bucket)
        """
        if not self.is_available:
            raise RuntimeError("R2 Storage nicht konfiguriert")

        key = self._build_key(candidate_id, filename)

        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=file_content,
                ContentType="application/pdf",
            )
            logger.info(f"CV hochgeladen: {key} ({len(file_content)} Bytes)")
            return key
        except ClientError as e:
            logger.error(f"R2 Upload fehlgeschlagen fuer {key}: {e}")
            raise

    def download_cv(self, key: str) -> bytes:
        """
        Laedt einen CV aus R2 herunter.

        Args:
            key: R2 Object Key (z.B. 'cvs/{uuid}.pdf')

        Returns:
            PDF-Bytes
        """
        if not self.is_available:
            raise RuntimeError("R2 Storage nicht konfiguriert")

        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            content = response["Body"].read()
            logger.debug(f"CV heruntergeladen: {key} ({len(content)} Bytes)")
            return content
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "NoSuchKey":
                logger.warning(f"CV nicht gefunden in R2: {key}")
                return None
            logger.error(f"R2 Download fehlgeschlagen fuer {key}: {e}")
            raise

    def delete_cv(self, key: str) -> bool:
        """
        Loescht einen CV aus R2.

        Args:
            key: R2 Object Key

        Returns:
            True wenn erfolgreich
        """
        if not self.is_available:
            return False

        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
            logger.info(f"CV geloescht: {key}")
            return True
        except ClientError as e:
            logger.error(f"R2 Loeschung fehlgeschlagen fuer {key}: {e}")
            return False

    def cv_exists(self, key: str) -> bool:
        """Prueft ob ein CV in R2 existiert."""
        if not self.is_available:
            return False

        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError:
            return False
