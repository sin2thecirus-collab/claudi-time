# Akquise-Seite — Fragen und Reparaturen

> **Arbeits-Datei fuer die gemeinsame Durchsicht der Akquise-Seite mit Milad**
> Wir gehen Stueck fuer Stueck durch, testen alles, und reparieren was kaputt ist.
> **Letzte Aktualisierung:** 01.03.2026

---

## Erledigte Reparaturen

### Reparatur 1: Testmodus deaktiviert
- **Status:** ERLEDIGT (01.03.2026)
- **Problem:** Der Testmodus war noch aktiv. Im Testmodus werden E-Mails umgeleitet und ein gelber Banner angezeigt.
- **Loesung:** Testmodus in der Datenbank auf `false` gesetzt.
- **Auswirkung:** Die Akquise-Seite arbeitet jetzt im echten Modus (E-Mails gehen an echte Empfaenger, kein gelber Banner mehr).

---

### Reparatur 2: ALLE Buttons auf der Akquise-Seite repariert (Alpine.js Bug)
- **Status:** ERLEDIGT (01.03.2026)
- **Commit:** b576b25 (gepusht auf main, Railway deployed automatisch)
- **Gemeldet von:** Milad ("der Button E-Mail versenden reagiert ueberhaupt nicht")

#### Was war das Problem?
Die Akquise-Seite benutzt eine Technik namens "Alpine.js" fuer interaktive Funktionen. Dabei gibt es zwei getrennte Bereiche:

1. **Die Hauptseite** (mit Tabs, E-Mail-Fenster, Rueckruf-Popup etc.)
2. **Der Anruf-Bildschirm** (mit Dispositionen, Notizen, Timer etc.)

Viele Buttons (z.B. "E-Mail senden", "Zurueck zur Liste", "Naechster Lead") mussten der Hauptseite einen Befehl geben. Dafuer suchten sie nach der Hauptseite mit einer Art "finde das erste Element mit Daten". Das Problem: Wenn der Anruf-Bildschirm geoeffnet war, fand diese Suche den **Anruf-Bildschirm** statt der **Hauptseite** — und der kannte die Befehle natuerlich nicht. Der Button klickte ins Leere.

#### Was wurde repariert?
Die Hauptseite hat jetzt einen eindeutigen Namen ("akquise-root") bekommen. Alle Buttons suchen jetzt gezielt nach diesem Namen statt "irgendein Element". Das funktioniert immer zuverlaessig, egal welches Fenster gerade offen ist.

#### Welche 8 Dateien wurden geaendert?

| # | Datei | Was wurde geaendert |
|---|-------|---------------------|
| 1 | `akquise_page.html` | Hauptseite hat jetzt den eindeutigen Namen `akquise-root` bekommen |
| 2 | `call_screen.html` | 6 Buttons repariert: Zurueck-Button, andere Stellen der Firma, **E-Mail senden**, Zurueck zur Liste, Auto-Weiter nach Disposition, Auto-Weiter nach neue Stelle |
| 3 | `email_modal.html` | 3 Buttons repariert: X-Schliessen, Abbrechen, Schliessen nach Versand |
| 4 | `company_group.html` | 1 Button repariert: Klick auf einen Lead in der Firmenliste |
| 5 | `tab_heute.html` | 1 Button repariert: Klick auf eine faellige Wiedervorlage |
| 6 | `tab_nicht_erreicht.html` | 3 Buttons repariert: Klick auf Lead, Follow-up-Button, Break-up-Button |
| 7 | `tab_qualifiziert.html` | 2 Buttons repariert: Klick auf Lead, Tab-Aktualisierung nach Qualify |
| 8 | `rueckruf_popup.html` | 1 Button repariert: Klick auf eine Stelle im Rueckruf-Popup |

**Insgesamt: 18 Button-Aufrufe in 8 Dateien repariert.**

#### Was sollte jetzt alles funktionieren?
- "E-Mail senden" Button im Anruf-Bildschirm
- E-Mail-Fenster schliessen (X und Abbrechen)
- Nach E-Mail-Versand: Fenster schliesst sich
- "Zurueck zur Liste" Button
- Automatisches Weiterschalten zum naechsten Lead nach Disposition
- Klick auf Leads in allen Tabs (Heute, Nicht erreicht, Qualifiziert)
- Klick auf Wiedervorlagen
- Follow-up und Break-up Buttons im "Nicht erreicht" Tab
- Rueckruf-Popup: Klick auf Stellen

---

### Reparatur 3: Firmen-Gruppierung repariert — Stellen werden jetzt korrekt zusammengefasst
- **Status:** ERLEDIGT (01.03.2026)
- **Commit:** 95c7c53 (gepusht auf main, Railway deployed automatisch)
- **Gemeldet von:** Milad ("einige Unternehmen haben mehrere Stellen ausgeschrieben und diese sollten aufgeklappt angezeigt werden")

#### Was war das Problem?
Die Seite hat zuerst die einzelnen Stellen geladen (z.B. 50 Stueck pro Seite) und erst danach versucht, sie nach Firma zu sortieren. Das Problem: Wenn eine Firma z.B. 3 Stellen hat, aber diese Stellen unterschiedliche Prioritaeten haben, wurden sie in der Liste auseinandergerissen.

**Beispiel vorher:**
- ABC GmbH — Stelle "Buchhalter" (Prio 9) → Position 1
- XYZ AG — Stelle "Controller" (Prio 7) → Position 2
- ABC GmbH — Stelle "Lohnbuchhalter" (Prio 5) → Position 15
- ABC GmbH — Stelle "Sachbearbeiter" (Prio 2) → Position 40

Die 3 Stellen von ABC GmbH waren ueber die ganze Liste verteilt statt zusammen in einer aufklappbaren Gruppe.

#### Was wurde repariert?
Die Reihenfolge wurde umgedreht:
1. **Erst** werden ALLE Stellen geladen (nicht nur 50)
2. **Dann** werden sie nach Firma gruppiert
3. **Dann** werden die Firmen-Gruppen nach der hoechsten Prioritaet ihrer Stellen sortiert
4. **Zuletzt** wird die Seite auf Firmen-Gruppen-Ebene paginiert (50 Firmen pro Seite)

**Beispiel nachher:**
- ABC GmbH (3 Stellen) → aufklappbar, zeigt alle 3 Stellen zusammen
- XYZ AG (1 Stelle) → darunter

---

### Reparatur 4+5: HTMX/Alpine.js — Script-Reihenfolge + initTree (Zwischenloesung)
- **Status:** ERSETZT DURCH REPARATUR 6
- **Commits:** 2aeaf27 (Script-Reihenfolge) + 5c7e93d (Alpine.initTree) + 12b9e5c (destroyTree+initTree)
- **Gefunden durch:** Systematischer Browser-Test (Textbausteine reagierten nicht → Console: 45 Fehler → Tiefenanalyse)

Diese Reparatur war eine Zwischenloesung. Das Problem wurde in **Reparatur 6** final geloest (siehe unten).

---

### Reparatur 6: Alpine/HTMX Timing-Bug — Architekturelle Loesung (FINAL)
- **Status:** ERLEDIGT (01.03.2026)
- **Commit:** 693e043 (gepusht auf main, Railway deployed automatisch)
- **Gefunden durch:** Tiefenanalyse — destroyTree+initTree (Reparatur 4+5) funktionierte NICHT zuverlässig

#### Was war das Problem?

HTMX laedt den Call-Screen und das E-Mail-Modal per `innerHTML`-Swap nach. Das hat **zwei fatale Konsequenzen:**

1. **`<script>` Tags werden NICHT ausgefuehrt:** HTMX `innerHTML` fuegt HTML ein, aber `<script>`-Bloecke im neuen HTML werden ignoriert. Die Funktionen `callScreen()` und `emailModal()` waren daher nie definiert wenn Alpine sie brauchte.

2. **Alpine's MutationObserver "verbraucht" Direktiven beim ersten Durchlauf:** Alpine erkennt das neue `<div x-data="callScreen()">` sofort via MutationObserver, liest `@click`, `x-model`, `x-show` etc. und verarbeitet sie. Nach diesem ersten Durchlauf sind die Direktiven "verbraucht" — kein `destroyTree + initTree` kann sie nochmal binden, weil Alpine die Attribute bereits konsumiert hat.

**5 gescheiterte Loesungsansaetze:**
1. `destroyTree + initTree` → Teilweise init, Direktiven schon verbraucht
2. Aggressives manuelles Cleanup (alle `_x_*` Properties loeschen) + initTree → Funktionierte nicht
3. `Alpine.deferMutations() + flushAndStopDeferringMutations()` → Daten zugaenglich, Direktiven nicht gebunden
4. `Alpine.mutateDom()` → Schlimmer, nichts initialisiert
5. `createContextualFragment` → Gleich wie deferMutations

#### Die Loesung: Logik von Daten trennen

Die Funktionsdefinitionen (`callScreen()`, `emailModal()`) wurden aus den HTMX-Partials in die **Hauptseite** (`akquise_page.html`) verschoben. Jinja-spezifische Variablen werden ueber `<script type="application/json">` Daten-Bloecke uebergeben (diese werden von HTMX nicht ignoriert, da sie nicht ausgefuehrt werden muessen — sie speichern nur Daten).

**Ablauf VORHER (kaputt):**
```
HTMX laedt call_screen.html → <script>function callScreen(){...}</script> wird IGNORIERT
→ Alpine sieht x-data="callScreen()" → ReferenceError: callScreen is not defined
```

**Ablauf NACHHER (funktioniert):**
```
akquise_page.html hat callScreen() IMMER definiert (beim Seitenstart geladen)
→ HTMX laedt call_screen.html → <script type="application/json"> hat Job-Daten
→ Alpine sieht x-data="callScreen()" → Funktion IST definiert → liest JSON-Daten → ALLES funktioniert
```

#### Was wurde geaendert?

| # | Datei | Aenderung |
|---|-------|-----------|
| 1 | `call_screen.html` | ~195 Zeilen `<script>function callScreen()...</script>` ersetzt durch 9-zeiliges JSON Daten-Block `<script id="call-screen-init" type="application/json">` |
| 2 | `email_modal.html` | ~76 Zeilen `<script>function emailModal()...</script>` ersetzt durch 12-zeiliges JSON Daten-Block `<script id="email-modal-init" type="application/json">` |
| 3 | `akquise_page.html` | `callScreen()` und `emailModal()` Funktionen hinzugefuegt (lesen Config aus JSON). Alle `destroyTree + initTree` Aufrufe entfernt (4 Stellen: loadTab, openCallScreen, openEmailModal, startCalling) |

**MERKE fuer zukuenftige Arbeit:** Bei HTMX-geladenen Alpine-Komponenten:
1. Alpine-Funktionsdefinitionen IMMER in der Hauptseite platzieren (die beim Seitenstart geladen wird)
2. Dynamische Daten (Jinja-Variablen) als `<script type="application/json">` Daten-Block uebergeben
3. NIEMALS `<script>function xyz()...</script>` in HTMX-Partials — wird ignoriert!
4. `destroyTree + initTree` ist KEINE Loesung — Alpine verbraucht Direktiven beim ersten MutationObserver-Durchlauf

---

## Noch zu pruefen (gemeinsam mit Milad)

### Seite allgemein
- [x] Seite laedt korrekt
- [x] Alle 6 Tabs wechseln (Heute 340, Neue Leads 327, Wiedervorlagen 2, Nicht erreicht 3, Qualifiziert 3, Archiv 9)
- [ ] Suchfeld funktioniert
- [ ] CSV-Import funktioniert

### Leads und Navigation
- [x] Leads in der Liste anklickbar → Anruf-Bildschirm oeffnet sich
- [x] Wiedervorlagen anklickbar
- [x] "Zurueck zur Liste" Button im Anruf-Bildschirm
- [x] Naechster Lead wird nach Disposition automatisch geladen (advanceToNextLead wird aufgerufen)
- [x] "1 weitere Stelle" Button wechselt zum anderen Job derselben Firma
- [ ] Rueckruf-Popup funktioniert bei Rueckruf-Suche

### Anruf-Bildschirm
- [x] Timer startet nach startCall() — zeigt "00:13" oben rechts neben Telefon-Button
- [x] Timer stoppt nach stopCall() — bleibt bei letztem Wert stehen
- [x] Telefonnummer wird angezeigt und ist klickbar (tel:-Link)
- [x] Ansprechpartner werden angezeigt (4x Nathalie Streitberger bei nicko cruises)
- [x] "+ Neuer AP" Button vorhanden
- [x] Notizen: Textbausteine fuegen Text korrekt ein (getestet: alle 5)
- [x] Textbaustein "AB besprochen" → "AB besprochen am 01.03.2026."
- [x] Textbaustein "Sekretariat" → "Sekretariat erreicht."
- [x] Textbaustein "Termin" → "Termin vereinbart: "
- [x] Textbaustein "Im Urlaub" → "AP im Urlaub bis "
- [x] Textbaustein "Keine DW" → "Keine Durchwahl verfuegbar."
- [x] Checkboxen (Qualifizierung) funktionieren — updates `quali` Objekt korrekt
- [ ] Anruf-Historie wird angezeigt (erst nach echtem Anruf testbar)

### Dispositionen (alle 14 getestet — Fetch-Interceptor, keine echten Daten geaendert)
- [x] D1a: Nicht erreicht → `nicht_erreicht` + Batch-Confirm fuer andere Stellen
- [x] D1b: AB besprochen → `mailbox_besprochen`
- [x] D2: Besetzt → `besetzt`
- [x] D3: Falsche Nummer → `falsche_nummer`
- [x] D4: Sekretariat → `sekretariat`
- [x] D5: Kein Bedarf → `kein_bedarf`
- [x] D6: Nie wieder → `nie_wieder` + confirm("Firma DAUERHAFT auf Blacklist?") VOR Ausfuehrung
- [x] D7: Interesse spaeter → oeffnet Follow-Up Panel (Datum + Uhrzeit + Notiz) → `interesse_spaeter` mit follow_up_date
- [x] D8: Will Infos (E-Mail) → `will_infos`
- [x] D9: Qualifiziert (Zweitkontakt) → oeffnet Follow-Up Panel → `qualifiziert_erst` mit follow_up_date
- [x] D10: Voll qualifiziert (ATS) → `voll_qualifiziert`
- [x] D11: AP nicht mehr da → `ap_nicht_mehr_da`
- [x] D12: Andere Stelle offen → oeffnet "Neue Stelle anlegen" Formular (Position + Art + Notizen)
- [x] D13: Weiterverbunden → `weiterverbunden`
- [x] Alle Dispositionen senden `contact_id` korrekt mit
- [x] Batch-Confirm erscheint bei Nicht-erreicht-Dispositionen fuer andere Stellen derselben Firma

### E-Mail-Funktionen
- [x] "E-Mail senden" Button im Anruf-Bildschirm oeffnet E-Mail-Modal (HTMX-Load)
- [x] E-Mail-Modal initialisiert korrekt: emailModal() Funktion aus Parent-Page + JSON-Daten aus Partial
- [x] Von-Dropdown zeigt Mailbox mit Limit (hamdard@sincirus.com 100/100)
- [x] An-Dropdown zeigt Kontakt mit E-Mail (Nathalie Streitberger <jobs@nicko-cruises.de>)
- [x] E-Mail-Typ waehlbar: Erst-Mail / Follow-up / Break-up
- [x] "E-Mail per GPT generieren" Button vorhanden (generateDraft Funktion definiert)
- [x] Abbrechen + X-Schliessen Buttons funktionieren
- [ ] GPT-Entwurf wird generiert (braucht echten Klick — verbraucht OpenAI Credits)
- [ ] Betreff und Text sind editierbar (erst nach GPT-Draft sichtbar)
- [ ] E-Mail-Versand funktioniert (2h Verzoegerung aktiv — braucht echten Test)
- [ ] Follow-up Button im "Nicht erreicht" Tab
- [ ] Break-up Button im "Nicht erreicht" Tab

### Qualifizierung
- [x] Checkliste im Anruf-Bildschirm funktioniert (5 Erstkontakt + 12 Zweitkontakt Checkboxen)
- [ ] "ATS-Stelle erstellen" Button funktioniert (nur bei D10 Voll qualifiziert)

---

## Offene Fragen

*(Hier werden Fragen gesammelt, die waehrend der gemeinsamen Durchsicht auftauchen)*

| # | Frage | Antwort | Status |
|---|-------|---------|--------|
| | | | |

---

## Notizen

- Die Reparatur der 18 Buttons wurde am 01.03.2026 deployed (Commit b576b25)
- Railway deployt automatisch nach Git Push — die Fixes sollten bereits live sein
- Der Testmodus wurde deaktiviert — E-Mails gehen jetzt an echte Empfaenger!
- **Reparatur 4+5** (HTMX/Alpine 2-Schichten-Bug, Zwischenloesung) — ersetzt durch Reparatur 6
- **Reparatur 6** (HTMX/Alpine Timing-Bug FINAL) am 01.03.2026 deployed:
  - Commit 693e043: callScreen() + emailModal() aus Partials ins Parent-Page verschoben
  - Jinja-Variablen via `<script type="application/json">` Daten-Bloecke
  - Alle `destroyTree + initTree` Workarounds entfernt
  - FINALE Loesung: Funktion ist IMMER definiert wenn Alpine sie braucht
- **Reparatur 7** (Auflegen-Button — Timer stoppt nicht nach Webex-Anruf) am 01.03.2026:
  - Problem: Timer lief endlos weiter nachdem der Webex-Anruf aufgelegt wurde
  - Loesung: Roter "Auflegen"-Button neben dem Timer, stoppt Timer manuell
  - Nach Auflegen: Timer-Wert + "beendet" wird angezeigt (bis Disposition geklickt)
  - `stopCall()` speichert Dauer in `callDuration`, zweiter Aufruf berechnet nicht neu
  - Geaenderte Dateien: `akquise_page.html` (callDuration + stopCall-Logik), `call_screen.html` (Auflegen-Button + beendet-Anzeige)
- **Reparatur 8** (Kontakt-Auswahl — falscher AP bei mehreren Ansprechpartnern) am 01.03.2026:
  - Problem: Gruener Button wahlte IMMER contacts[0]. Klick auf andere AP-Nummer startete keinen Timer und aenderte contactId nicht.
  - Loesung: `selectAndCall(contactId, phone)` — aktualisiert contactId + selectedPhone + startet Timer
  - Gruener Button zeigt jetzt dynamisch die zuletzt gewaehlte Nummer (Alpine x-text)
  - Kontaktliste: @click auf Telefonnummer ruft selectAndCall() + tel:-Link oeffnet Webex
  - E-Mail-Button nutzt jetzt dynamische contactId statt hardcoded contacts[0]
- **Reparatur 9** (Job-Text unstrukturiert — reiner Text-Blob) am 01.03.2026:
  - Problem: job_text wurde als weisser Text-Block angezeigt, keine Struktur erkennbar
  - Loesung: Server-seitiger Parser `_parse_job_sections()` erkennt deutsche Sektions-Header
  - Erkannte Sektionen: Aufgaben, Anforderungen, Unternehmen, Wir bieten, Kontakt
  - Jede Sektion bekommt Farbbalken + Titel + Trennlinie
  - Fallback: Wenn keine Sektionen erkannt, wird Text-Blob wie bisher angezeigt
