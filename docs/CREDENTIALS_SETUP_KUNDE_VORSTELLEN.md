# Credential-Setup: "Kunde Vorstellen" Feature

Anleitung zum Einrichten aller Credentials in n8n fuer die 4 Workflows.

**n8n URL:** https://n8n-production-aa9c.up.railway.app

---

## Uebersicht: Welche Credentials werden benoetigt?

| # | Credential-Typ | Fuer Workflow | Status |
|---|----------------|---------------|--------|
| 1 | Microsoft Outlook OAuth2 | WF1 + WF4 (E-Mail senden) | Bereits vorhanden (`JM5L45MlrvzYZRUR`) |
| 2 | SMTP (IONOS) — hamdard@sincirus-karriere.de | WF1 + WF4 (E-Mail senden) | Bereits vorhanden (`AcpnjURgMRFwkcG1`) |
| 3 | SMTP (IONOS) — m.hamdard@sincirus-karriere.de | WF1 + WF4 (E-Mail senden) | NEU ERSTELLEN |
| 4 | SMTP (IONOS) — m.hamdard@jobs-sincirus.de | WF1 + WF4 (E-Mail senden) | NEU ERSTELLEN |
| 5 | SMTP (IONOS) — hamdard@jobs-sincirus.de | WF1 + WF4 (E-Mail senden) | NEU ERSTELLEN |
| 6 | IMAP (E-Mail lesen) | WF3 (Antwort verarbeiten) | NEU ERSTELLEN |
| 7 | OpenAI API Key | WF3 (KI-Klassifizierung) | NEU ERSTELLEN |

---

## Schritt 1: Fehlende IONOS SMTP Credentials erstellen

Fuer jede fehlende IONOS-Mailbox:

1. Gehe zu **n8n → Settings → Credentials → Add Credential**
2. Suche nach **"SMTP"**
3. Fuelle aus:

### m.hamdard@sincirus-karriere.de

| Feld | Wert |
|------|------|
| Name | `IONOS SMTP - m.hamdard@sincirus-karriere.de` |
| Host | `smtp.ionos.de` |
| Port | `587` |
| SSL/TLS | `STARTTLS` |
| User | `m.hamdard@sincirus-karriere.de` |
| Password | *(dein IONOS-Passwort)* |

### m.hamdard@jobs-sincirus.de

| Feld | Wert |
|------|------|
| Name | `IONOS SMTP - m.hamdard@jobs-sincirus.de` |
| Host | `smtp.ionos.de` |
| Port | `587` |
| SSL/TLS | `STARTTLS` |
| User | `m.hamdard@jobs-sincirus.de` |
| Password | *(dein IONOS-Passwort)* |

### hamdard@jobs-sincirus.de

| Feld | Wert |
|------|------|
| Name | `IONOS SMTP - hamdard@jobs-sincirus.de` |
| Host | `smtp.ionos.de` |
| Port | `587` |
| SSL/TLS | `STARTTLS` |
| User | `hamdard@jobs-sincirus.de` |
| Password | *(dein IONOS-Passwort)* |

> **Tipp:** Klicke nach dem Speichern auf "Test" um zu pruefen, ob die Verbindung funktioniert.

---

## Schritt 2: IMAP Credential erstellen (fuer Antwort-Erkennung)

Workflow 3 ("Antwort verarbeiten") braucht IMAP-Zugang um eingehende E-Mails zu lesen.

1. Gehe zu **n8n → Settings → Credentials → Add Credential**
2. Suche nach **"IMAP"**
3. Fuelle aus:

| Feld | Wert |
|------|------|
| Name | `IMAP - hamdard@sincirus.com` |
| Host | `outlook.office365.com` |
| Port | `993` |
| SSL/TLS | `true` |
| User | `hamdard@sincirus.com` |
| Password | *(App-Passwort oder OAuth2 Token)* |

> **Hinweis:** Fuer Outlook/Microsoft 365 brauchst du moeglicherweise ein App-Passwort
> oder OAuth2-basierte Authentifizierung. Standard-Passwort funktioniert oft nicht
> wegen Microsoft-Sicherheitsrichtlinien.
>
> Alternative: IONOS IMAP nutzen (`imap.ionos.de`, Port 993, SSL).

---

## Schritt 3: OpenAI API Key einrichten

Workflow 3 nutzt GPT-4o-mini fuer die automatische Klassifizierung von Kunden-Antworten.

1. Gehe zu **n8n → Settings → Credentials → Add Credential**
2. Suche nach **"OpenAI"**
3. Fuelle aus:

| Feld | Wert |
|------|------|
| Name | `OpenAI API` |
| API Key | *(dein OpenAI API Key von platform.openai.com)* |

> **Kosten:** GPT-4o-mini ist sehr guenstig (~0.15$/1M Input Tokens).
> Bei ~50 Antworten/Monat kostet das weniger als 0.10$/Monat.

---

## Schritt 4: Credentials den Workflow-Nodes zuweisen

### Workflow 1: "Kunde Vorstellen - E-Mail senden" (ID: 6H27MwNW3Z4VBbJS)

| Node | Credential zuweisen |
|------|---------------------|
| **Send via Outlook** | Microsoft Outlook OAuth2 (existierend) |
| **Send via IONOS SMTP** | IONOS SMTP - je nach gewaehlter Mailbox |

### Workflow 3: "Kunde Vorstellen - Antwort verarbeiten" (ID: wnKlwfiwwyR6XYRa)

| Node | Credential zuweisen |
|------|---------------------|
| **IMAP Email Trigger** | IMAP Credential (Schritt 2) |
| **KI Klassifizierung** | OpenAI API (Schritt 3) |

> **Wichtig:** Der IMAP Trigger ist aktuell **deaktiviert**. Nach dem Zuweisen
> des IMAP Credentials den Node aktivieren (Rechtsklick → Enable).

### Workflow 4: "Kunde Vorstellen - Fallback-Kaskade" (ID: UxTu8RNnFnrIPa4i)

| Node | Credential zuweisen |
|------|---------------------|
| **Send via Outlook FB** | Microsoft Outlook OAuth2 (existierend) |
| **Send via IONOS FB** | IONOS SMTP (die Haupt-Mailbox) |

---

## Schritt 5: Workflows aktivieren

Nachdem alle Credentials zugewiesen und getestet sind:

1. **WF1** "Kunde Vorstellen - E-Mail senden" → **Aktivieren**
2. **WF2** "Kunde Vorstellen - Follow-Up Sequenz" → **Aktivieren** (braucht keine eigenen Credentials)
3. **WF3** "Kunde Vorstellen - Antwort verarbeiten" → **Aktivieren** (IMAP Node vorher enablen!)
4. **WF4** "Kunde Vorstellen - Fallback-Kaskade" → **Aktivieren**

Reihenfolge: WF1 → WF2 → WF3 → WF4

---

## Schritt 6: End-to-End Test

1. Oeffne das Match Center in der App
2. Klicke auf einen Match → "Kunde vorstellen"
3. Waehle einen Kontakt und eine Mailbox
4. Klicke "KI-Vorstellung generieren"
5. Pruefe den generierten Text und klicke "Senden"
6. Pruefe in n8n ob die Execution erfolgreich war
7. Pruefe ob die E-Mail im Postfach des Empfaengers angekommen ist

---

## Umgebungsvariablen in der App (Railway)

Stelle sicher, dass folgende Variablen in Railway konfiguriert sind:

| Variable | Wert | Beschreibung |
|----------|------|--------------|
| `N8N_WEBHOOK_URL` | `https://n8n-production-aa9c.up.railway.app` | n8n Base-URL |
| `N8N_API_TOKEN` | *(dein Token)* | Bearer-Token fuer n8n Callbacks |
| `ANTHROPIC_API_KEY` | *(dein Key)* | Claude API fuer E-Mail-Generierung |
