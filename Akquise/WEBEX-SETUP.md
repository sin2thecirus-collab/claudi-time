# Webex-Integration — Setup-Anleitung

> **Stand: 28.02.2026**
> **Voraussetzung:** Webex Calling (Business) + Admin-Zugang

---

## UEBERSICHT

Es gibt **2 Webex-Workflows** auf n8n:

| # | Workflow | n8n ID | Zweck | Status |
|---|---------|--------|-------|--------|
| 1 | Eingehender Anruf | V8rGRTK0gCkqhWvV | Echtzeit-Popup im Browser bei eingehendem Anruf | **AKTIV + GETESTET** |
| 2 | Recording → Whisper → GPT | w5LTbRw1bFSCIKNP | Anruf-Aufzeichnung transkribieren + analysieren | **INAKTIV — braucht OAuth** |

---

## SCHRITT 1: Webex Telephony Webhook (Eingehende Anrufe)

Dieser Webhook feuert in Echtzeit wenn jemand dich anruft oder du jemanden anrufst.

### 1.1 Webhook erstellen (developer.webex.com)

1. Gehe zu: **https://developer.webex.com/my-apps**
2. Klicke **"Create a Webhook"** (oder nutze die API)
3. Konfiguration:
   - **Name:** `Pulspoint Incoming Call`
   - **Target URL:** `https://n8n-production-aa9c.up.railway.app/webhook/webex-incoming-call`
   - **Resource:** `telephony_calls`
   - **Event:** `created` (oder `all`)
   - **Filter:** (leer lassen = alle Anrufe)

### 1.2 Alternativ per API (schneller):

```bash
curl -X POST "https://webexapis.com/v1/webhooks" \
  -H "Authorization: Bearer DEIN_WEBEX_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Pulspoint Incoming Call",
    "targetUrl": "https://n8n-production-aa9c.up.railway.app/webhook/webex-incoming-call",
    "resource": "telephony_calls",
    "event": "created"
  }'
```

### 1.3 Testen

Nachdem der Webhook erstellt ist, rufe dich selbst an. Im Browser unter `/akquise` sollte ein Popup erscheinen (oder ein Toast "nicht zugeordnet" wenn die Nummer unbekannt ist).

---

## SCHRITT 2: Webex OAuth Token (fuer Recording-Workflow)

Der Recording-Workflow braucht einen OAuth Refresh-Token um Aufzeichnungen herunterzuladen.

### 2.1 Integration erstellen (einmalig)

1. Gehe zu: **https://developer.webex.com/my-apps**
2. Klicke **"Create a New App"** → **"Integration"**
3. Konfiguration:
   - **Name:** `Pulspoint Recording`
   - **Redirect URI:** `https://n8n-production-aa9c.up.railway.app/webhook/webex-oauth-callback` (oder jede andere URL — du brauchst nur den Code)
   - **Scopes:** `recording:read` (mindestens), optional: `spark:all`
4. Nach dem Erstellen bekommst du:
   - **Client ID** (sollte sein: `Ca38bf73749ba4b89261dc2a7b33b30d7864467ca24116e39c0a0522442d5e8ae`)
   - **Client Secret** (sollte sein: `351a81fd0b05696ea5cef13fea22685c7271db842aa0e0ea14b76eed48037e30`)

### 2.2 Authorization Code holen

Oeffne diesen Link im Browser (ersetze CLIENT_ID falls anders):

```
https://webexapis.com/v1/authorize?client_id=Ca38bf73749ba4b89261dc2a7b33b30d7864467ca24116e39c0a0522442d5e8ae&response_type=code&redirect_uri=https://n8n-production-aa9c.up.railway.app/webhook/webex-oauth-callback&scope=recording:read%20spark:all&state=pulspoint
```

1. Melde dich mit deinem Webex-Account an
2. Klicke **"Zugriff erlauben"**
3. Du wirst weitergeleitet. Die URL enthaelt einen `?code=XXXXXX` Parameter
4. Kopiere diesen Code (gueltig nur 5 Minuten!)

### 2.3 Code gegen Refresh-Token tauschen

```bash
curl -X POST "https://webexapis.com/v1/access_token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=authorization_code&client_id=Ca38bf73749ba4b89261dc2a7b33b30d7864467ca24116e39c0a0522442d5e8ae&client_secret=351a81fd0b05696ea5cef13fea22685c7271db842aa0e0ea14b76eed48037e30&code=DEIN_CODE_HIER&redirect_uri=https://n8n-production-aa9c.up.railway.app/webhook/webex-oauth-callback"
```

Die Antwort enthaelt:
```json
{
  "access_token": "...",
  "refresh_token": "DAS_BRAUCHST_DU",
  "expires_in": 1209600,
  "refresh_token_expires_in": 7776000
}
```

### 2.4 Refresh-Token in n8n setzen

1. Gehe zu n8n: **https://n8n-production-aa9c.up.railway.app**
2. Settings → Environment Variables
3. Setze diese 3 Variablen:
   - `WEBEX_CLIENT_ID` = `Ca38bf73749ba4b89261dc2a7b33b30d7864467ca24116e39c0a0522442d5e8ae`
   - `WEBEX_CLIENT_SECRET` = `351a81fd0b05696ea5cef13fea22685c7271db842aa0e0ea14b76eed48037e30`
   - `WEBEX_REFRESH_TOKEN` = Der Token aus Schritt 2.3
4. Stelle sicher dass `OPENAI_API_KEY` auch gesetzt ist (fuer Whisper + GPT)
5. n8n neu starten (Settings → Restart, oder Railway Redeploy)

### 2.5 Recording-Webhook erstellen

```bash
curl -X POST "https://webexapis.com/v1/webhooks" \
  -H "Authorization: Bearer DEIN_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Pulspoint Call Recording",
    "targetUrl": "https://n8n-production-aa9c.up.railway.app/webhook/webex-recording",
    "resource": "recordings",
    "event": "created"
  }'
```

### 2.6 Recording-Workflow aktivieren

Nachdem OAuth-Token + Recording-Webhook konfiguriert sind:
1. Gehe zum Workflow **"Akquise: Webex Recording → Whisper → GPT → MT"** (ID: w5LTbRw1bFSCIKNP)
2. Aktiviere den Workflow (Toggle oben rechts)

---

## SCHRITT 3: Call Recording aktivieren (Webex Admin)

Damit Webex Anrufe automatisch aufzeichnet:

1. Gehe zu **https://admin.webex.com**
2. Navigation: **Services → Calling → Call Recording**
3. Aktiviere **"Automatic Call Recording"** fuer deinen Account
4. Waehle **"Record all calls"** oder **"Record on demand"**

**Hinweis:** Informiere den Gespraechspartner IMMER ueber die Aufzeichnung (§201 StGB).

---

## ARCHITEKTUR

```
                    ECHTZEIT-ANRUF
Webex Calling → telephony_calls Webhook
                    ↓
n8n Workflow V8rGRTK0gCkqhWvV (4 Nodes)
  → Telefonnummer extrahieren
  → Pruefen
  → POST /api/akquise/events/incoming-call
                    ↓
SSE Event-Bus → Browser Popup
                    ↓
Milad sieht: "Eingehender Anruf von [Firma]"
  → Klick auf Lead → Call-Screen oeffnet sich


                    NACH DEM ANRUF
Webex Recording → recordings Webhook
                    ↓
n8n Workflow w5LTbRw1bFSCIKNP (12 Nodes)
  → OAuth Token holen
  → Recording herunterladen
  → Whisper Transkription (deutsch)
  → GPT-4o-mini Daten-Extraktion
  → MT-Payload aufbereiten
                    ↓
    Candidate ID vorhanden?
    ├→ JA: PATCH /api/candidates/{id} (Kandidat aktualisieren)
    └→ NEIN: POST /api/akquise/unassigned-calls (Zwischenspeicher)
```

---

## TROUBLESHOOTING

### Webhook antwortet nicht (404)
- Workflow muss AKTIV sein (Toggle in n8n)
- Webhook-URL pruefen: `https://n8n-production-aa9c.up.railway.app/webhook/webex-incoming-call`

### 401 bei Backend-Call
- API-Key `X-API-Key` Header muss gesetzt sein

### OAuth Token abgelaufen
- Refresh-Token ist 90 Tage gueltig
- Der Workflow gibt den neuen Token in der Console aus
- Neuen Token in n8n Environment Variables aktualisieren

### Recording-Download fehlschlaegt
- Pruefen ob `recording:read` Scope im OAuth vorhanden ist
- Temporary Download Links sind ~24h gueltig

---

## n8n ENVIRONMENT VARIABLES (ALLE)

| Variable | Beschreibung | Wo setzen |
|----------|-------------|-----------|
| `WEBEX_CLIENT_ID` | Webex Integration Client ID | n8n Settings → Environment |
| `WEBEX_CLIENT_SECRET` | Webex Integration Client Secret | n8n Settings → Environment |
| `WEBEX_REFRESH_TOKEN` | Webex OAuth Refresh Token | n8n Settings → Environment |
| `OPENAI_API_KEY` | OpenAI API Key (Whisper + GPT) | n8n Settings → Environment |
