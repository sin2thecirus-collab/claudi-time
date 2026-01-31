"""Cloudflare R2 Object Storage Service fuer CV-Dateien.

Ordnerstruktur im Bucket:
    finance/
        Mueller_Thomas_a3f2b1c4/
            Lebenslauf_Mueller_Thomas.pdf
    engineering/
        Weber_Klaus_b2c3d4e5/
            Lebenslauf_Weber_Klaus.pdf
    sonstige/
        Doe_Jane_f6g7h8i9/
            Lebenslauf_Doe_Jane.pdf
"""

import logging
import re
import unicodedata

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


def _sanitize_name(name: str) -> str:
    """
    Bereinigt einen Namen fuer Dateisystem-kompatible Pfade.

    - Umlaute werden aufgeloest (ä→ae, ü→ue, ö→oe, ß→ss)
    - Sonderzeichen werden entfernt
    - Leerzeichen werden zu Unterstrichen
    """
    if not name:
        return "Unbekannt"

    # Deutsche Umlaute manuell ersetzen
    replacements = {
        "ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss",
        "Ä": "Ae", "Ö": "Oe", "Ü": "Ue",
    }
    for char, replacement in replacements.items():
        name = name.replace(char, replacement)

    # Unicode-Akzente entfernen (é→e, ñ→n, etc.)
    name = unicodedata.normalize("NFD", name)
    name = "".join(c for c in name if unicodedata.category(c) != "Mn")

    # Nur Buchstaben, Zahlen, Leerzeichen und Bindestriche behalten
    name = re.sub(r"[^a-zA-Z0-9\s\-]", "", name)

    # Mehrfache Leerzeichen zu einem
    name = re.sub(r"\s+", " ", name).strip()

    # Leerzeichen zu Unterstrichen
    name = name.replace(" ", "_")

    return name or "Unbekannt"


def _category_to_folder(hotlist_category: str | None) -> str:
    """Mappt die Hotlist-Kategorie auf den R2-Ordnernamen."""
    if not hotlist_category:
        return "sonstige"

    mapping = {
        "FINANCE": "finance",
        "ENGINEERING": "engineering",
    }
    return mapping.get(hotlist_category.upper(), "sonstige")


class R2StorageService:
    """Service fuer Upload/Download von CVs in Cloudflare R2.

    Ordnerstruktur:
        {kategorie}/{Nachname}_{Vorname}_{uuid_kurz}/Lebenslauf_{Nachname}_{Vorname}.pdf

    Beispiel:
        finance/Mueller_Thomas_a3f2b1c4/Lebenslauf_Mueller_Thomas.pdf
    """

    def __init__(self):
        self.client = _get_s3_client()
        self.bucket = settings.r2_bucket_name

    @property
    def is_available(self) -> bool:
        """Prueft ob R2 konfiguriert und verfuegbar ist."""
        return self.client is not None

    def build_cv_key(
        self,
        candidate_id: str,
        first_name: str | None = None,
        last_name: str | None = None,
        hotlist_category: str | None = None,
    ) -> str:
        """
        Baut den R2-Object-Key (Pfad) fuer einen CV.

        Format: {kategorie}/{Nachname}_{Vorname}_{uuid_kurz}/Lebenslauf_{Nachname}_{Vorname}.pdf

        Args:
            candidate_id: UUID des Kandidaten
            first_name: Vorname
            last_name: Nachname
            hotlist_category: FINANCE / ENGINEERING / None

        Returns:
            R2 Object Key z.B. 'finance/Mueller_Thomas_a3f2b1c4/Lebenslauf_Mueller_Thomas.pdf'
        """
        folder = _category_to_folder(hotlist_category)
        safe_first = _sanitize_name(first_name or "")
        safe_last = _sanitize_name(last_name or "")

        # Kurze UUID (erste 8 Zeichen) fuer Eindeutigkeit
        uuid_short = str(candidate_id).replace("-", "")[:8]

        # Ordnername: Nachname_Vorname_uuid
        if safe_last and safe_first:
            candidate_folder = f"{safe_last}_{safe_first}_{uuid_short}"
            filename = f"Lebenslauf_{safe_last}_{safe_first}.pdf"
        elif safe_last:
            candidate_folder = f"{safe_last}_{uuid_short}"
            filename = f"Lebenslauf_{safe_last}.pdf"
        elif safe_first:
            candidate_folder = f"{safe_first}_{uuid_short}"
            filename = f"Lebenslauf_{safe_first}.pdf"
        else:
            candidate_folder = f"Kandidat_{uuid_short}"
            filename = f"Lebenslauf_{uuid_short}.pdf"

        return f"{folder}/{candidate_folder}/{filename}"

    def upload_cv(
        self,
        candidate_id: str,
        file_content: bytes,
        first_name: str | None = None,
        last_name: str | None = None,
        hotlist_category: str | None = None,
    ) -> str:
        """
        Laedt einen CV nach R2 hoch.

        Args:
            candidate_id: UUID des Kandidaten
            file_content: PDF-Bytes
            first_name: Vorname des Kandidaten
            last_name: Nachname des Kandidaten
            hotlist_category: FINANCE / ENGINEERING / None

        Returns:
            R2 Object Key (Pfad im Bucket)
        """
        if not self.is_available:
            raise RuntimeError("R2 Storage nicht konfiguriert")

        key = self.build_cv_key(candidate_id, first_name, last_name, hotlist_category)

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

    def download_cv(self, key: str) -> bytes | None:
        """
        Laedt einen CV aus R2 herunter.

        Args:
            key: R2 Object Key

        Returns:
            PDF-Bytes oder None wenn nicht gefunden
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
        """Loescht einen CV aus R2."""
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
