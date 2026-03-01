# Webex-Integration — Status + Referenz

> **Stand: 28.02.2026 — ALLES FERTIG + AKTIV**
> Alle Schritte wurden automatisch erledigt. Diese Datei dient nur als Referenz.

---

## STATUS: KOMPLETT

| # | Workflow | n8n ID | Zweck | Status |
|---|---------|--------|-------|--------|
| 1 | Eingehender Anruf | V8rGRTK0gCkqhWvV | Echtzeit-Popup im Browser bei eingehendem Anruf | **AKTIV + GETESTET** |
| 2 | Recording → Whisper → GPT | TIZR3L_9arwlI4O69wTMY | Anruf-Aufzeichnung transkribieren + analysieren | **AKTIV + PRODUKTIV** |

**Was erledigt ist:**
- Telephony-Webhook bei Webex REGISTRIERT (28.02.2026)
- Recording-Webhook bei Webex REGISTRIERT (schon vorher aktiv)
- OAuth Refresh-Token aktualisiert (28.02.2026) mit `recordings_write` Scope
- Scopes: `spark:recordings_read spark:recordings_write spark:people_read spark:calls_read`
- DSGVO: Recording-DELETE funktioniert jetzt (dank `recordings_write`)
- 3 redundante/temp Workflows geloescht (w5LTbRw1bFSCIKNP, g7ZNmN7U3QcX0NBi, EHlLdm8ojIZpbq1X)

---

## ARCHITEKTUR

```
                    ECHTZEIT-ANRUF
Webex Calling → telephony_calls Webhook
                    ↓
n8n Workflow V8rGRTK0gCkqhWvV (4 Nodes)
  → Telefonnummer extrahieren
  → Pruefen (mind. 4 Zeichen)
  → POST /api/akquise/events/incoming-call
                    ↓
SSE Event-Bus → Browser Popup
                    ↓
Milad sieht: "Eingehender Anruf von [Firma]"
  → Klick auf Lead → Call-Screen oeffnet sich


                    NACH DEM ANRUF
Webex Recording → recordings Webhook
                    ↓
n8n Workflow TIZR3L_9arwlI4O69wTMY (14 Nodes)
  → OAuth Token holen (Refresh Token im Code-Node)
  → Recording herunterladen
  → Whisper Transkription (deutsch)
  → GPT-4o-mini Daten-Extraktion
  → MT-Payload aufbereiten
                    ↓
    Candidate ID vorhanden?
    ├→ JA: PATCH /api/candidates/{id} (Kandidat aktualisieren)
    └→ NEIN: POST /api/akquise/unassigned-calls (Zwischenspeicher)
                    ↓
    Recording bei Webex loeschen (DSGVO)
```

---

## WICHTIGE DETAILS

### OAuth-Token
- **Wo:** Hardcoded im Code-Node "Get Access Token" des Workflows TIZR3L_9arwlI4O69wTMY
- **NICHT** in Railway Environment Variables (weder Claudi-Time noch n8n)
- **Gueltig:** Refresh-Token ist 90 Tage gueltig (bis ca. Ende Mai 2026)
- **Rotation:** Webex kann bei Nutzung einen neuen Refresh-Token zurueckgeben — der Workflow loggt das

### Webex Integration (developer.webex.com)
- **Client ID:** `Ca38bf73749ba4b89261dc2a7b33b30d7864467ca24116e39c0a0522442d5e8ae`
- **Client Secret:** Im Code-Node des Workflows (nicht in ENV)
- **Redirect URI:** `https://n8n-production-aa9c.up.railway.app/webhook/webex-oauth-callback`

### Webhook-URLs
- **Incoming Call:** `https://n8n-production-aa9c.up.railway.app/webhook/webex-incoming-call`
- **Recording:** `https://n8n-production-aa9c.up.railway.app/webhook/156a46f7-bc92-4a7a-89bc-805c2ff43c64`

---

## TROUBLESHOOTING

### Webhook antwortet nicht (404)
- Workflow muss AKTIV sein (Toggle in n8n)

### 401 bei Backend-Call
- API-Key `X-API-Key` Header muss gesetzt sein

### OAuth Token abgelaufen (nach ~90 Tagen)
- Neuen OAuth-Flow durchfuehren:
  1. Temp-Callback-Workflow in n8n erstellen (Webhook → HTTP Request → Code)
  2. Authorization-URL im Browser oeffnen (mit allen Scopes)
  3. Neuen Refresh-Token aus Callback in den Code-Node des Recording-Workflows eintragen
- Alternativ: Claude Code kann das automatisch machen (wie am 28.02.2026 gemacht)

### Recording-DELETE fehlschlaegt (403)
- Pruefen ob `spark:recordings_write` Scope im OAuth vorhanden ist
- Wenn nicht: OAuth-Flow mit erweitertem Scope wiederholen

### Recording-Download fehlschlaegt
- Pruefen ob `spark:recordings_read` Scope vorhanden
- Temporary Download Links sind ~24h gueltig
