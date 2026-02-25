# PDF Profil Button — Technische Dokumentation

> **Stand: 25.02.2026 — Nach komplettem Template-Rewrite**

---

## WAS IST DER PDF PROFIL BUTTON?

Der Button auf der Kandidaten-Detailseite generiert ein professionelles A4-PDF im Sincirus-Branding.
Das PDF wird auch als E-Mail-Anhang bei Kandidaten-Vorstellungen an Kunden versendet.

**Zwei Seiten:**
- **Seite 1 (Deckblatt):** Hero-Header, Eckdaten, Kurzprofil, ERP & IT, Sprachen, Schluesselqualifikationen, Visitenkarte
- **Seite 2+ (Lebenslauf):** Berufserfahrung, Ausbildung, Zertifikate

---

## DATEIEN-UEBERSICHT

| Datei | Zweck |
|-------|-------|
| `app/templates/profile_sincirus_branded.html` | **DAS Template** — HTML + CSS das zu PDF wird |
| `app/services/profile_pdf_service.py` | **Service** — Laedt Kandidat aus DB, bereitet Daten auf, rendert Template |
| `preview_pdf_design.html` | **Target-Design** — So MUSS das PDF aussehen (im Browser oeffnen) |
| `app/static/images/sincirus_logo_komplett_white.png` | Weisses Logo fuer den Hero-Header |
| `app/static/images/sincirus_logo_komplett_transparent.png` | Transparentes Logo fuer CV-Header + Visitenkarte |
| `app/static/images/milad_foto.jpg` | Consultant-Foto in der Visitenkarte |
| `app/static/fonts/DMSans-Regular.ttf` | DM Sans Font (Regular) |
| `app/static/fonts/DMSans-Italic.ttf` | DM Sans Font (Italic) |
| `app/static/fonts/DMSerifDisplay-Regular.ttf` | DM Serif Display Font (Titel) |

---

## KRITISCHE FEHLER AUS VERGANGENEN SESSIONS

### 1. BROKEN CSS — Die gefaehrlichste Falle

**Was passiert ist:** Das CSS wurde beim Bearbeiten abgeschnitten. Die `.badge-conf` Klasse hatte kein schliessendes `}`. Das hat das CSS-Parsing fuer ALLE nachfolgenden Regeln zerstoert.

**Beispiel des kaputten CSS:**
```css
.badge-conf {
  font-size: 7.5px;
  font          /* <-- HIER ABGESCHNITTEN, KEIN } */

.hero-content {   /* <-- Diese und alle folgenden Klassen werden falsch geparst */
```

**Lektion:** Nach JEDER CSS-Aenderung pruefen:
- Jede Klasse hat ein oeffnendes `{` UND ein schliessendes `}`
- Keine abgeschnittenen Properties (z.B. `font` ohne Wert und Semikolon)
- Im Browser oeffnen und DevTools Console checken ob CSS-Parse-Errors gemeldet werden

### 2. RGBA vs HEX — WeasyPrint kann RGBA

**Was passiert ist:** Jemand hat alle `rgba()` Werte durch Hex-Farben ersetzt in der Annahme, WeasyPrint koennte kein rgba. Das stimmt NICHT.

**Falsch:**
```css
.hero-label { color: #d9eef5; }           /* Falscher Hex-Wert */
.hero-ref { background: #0a7dae; }        /* Komplett andere Farbe! */
```

**Richtig:**
```css
.hero-label { color: rgba(255,255,255,0.85); }        /* Transparentes Weiss */
.hero-ref { background: rgba(255,255,255,0.12); }     /* Subtil transparent */
```

**Lektion:** WeasyPrint unterstuetzt `rgba()`, `var()`, `linear-gradient()` und die meisten modernen CSS-Features. NICHT unnoetig in Hex umwandeln — das veraendert die Farben!

### 3. FEHLENDE CSS-Klassen

**Was passiert ist:** Die `.badge-date` Klasse fehlte komplett im Template, obwohl sie im HTML verwendet wurde. Das Element wurde ohne Styling gerendert.

**Lektion:** Wenn das Target-Design eine CSS-Klasse hat, MUSS sie auch im Template sein. Immer 1:1 abgleichen.

### 4. Visitenkarte zu klein dimensioniert

**Was passiert ist:** Alle Werte in der Visitenkarte wichen vom Target ab — Foto zu klein, Text zu klein, Abstande zu eng. Das hat die Visitenkarte "gequetscht" wirken lassen.

**Korrekte Werte (Stand 25.02.2026):**
```
.vcard-footer padding: 8px 48px 18px 48px
.vcard border-radius: 10px, border-top: 3px solid
.vcard-body padding: 18px 24px, gap: 18px
.vcard-photo: 82px (20% groesser als Target-Design 68px, auf Milads Wunsch)
.vcard-sep height: 64px
.vcard-name font-size: 15px
.vcard-title font-size: 10px, margin-bottom: 8px
.vcard-row font-size: 10px, gap: 8px
.vcard-icon font-size: 10px, width: 14px
.vcard-domain font-size: 10px, margin-top: 6px
.vcard-brand img height: 86px
```

### 5. Dekoratives Element vergessen

**Was passiert ist:** Der `.hero-header::after` Pseudo-Element (grosser dezenter Kreis oben rechts im Header) fehlte komplett.

**Korrekt:**
```css
.hero-header::after {
  content: '';
  position: absolute;
  right: -40px;
  top: -40px;
  width: 260px;
  height: 260px;
  border: 40px solid rgba(255,255,255,0.03);
  border-radius: 50%;
}
```

---

## SCHRITT-FUER-SCHRITT ARBEITSANLEITUNG

### Wie man das Template sicher bearbeitet

**Schritt 1: Target-Design im Browser oeffnen**
```
open preview_pdf_design.html
```
Das ist die EINZIGE Wahrheit. So MUSS das PDF aussehen.

**Schritt 2: Template-Datei lesen**
```
app/templates/profile_sincirus_branded.html
```
IMMER zuerst die gesamte Datei lesen. NIEMALS aus dem Gedaechtnis arbeiten.

**Schritt 3: Aenderung machen**

**Schritt 4: Test-HTML generieren und vergleichen**
```python
# Dieses Script rendert das Template mit Mock-Daten als HTML-Vorschau
python3 -c "
import os, base64
from jinja2 import Environment, FileSystemLoader

static_dir = os.path.abspath('app/static')
template_dir = os.path.abspath('app/templates')

def logo_data_uri(path):
    try:
        with open(path, 'rb') as f:
            return f'data:image/png;base64,{base64.b64encode(f.read()).decode()}'
    except FileNotFoundError:
        return ''

env = Environment(loader=FileSystemLoader(template_dir))
template = env.get_template('profile_sincirus_branded.html')

context = {
    'today_date': '25. Februar 2026',
    'candidate_ref': 'SP-2026-TEST',
    'hero_title': 'Bilanzbuchhalter (IHK)',
    'hero_meta': 'Vollzeit &nbsp;&middot;&nbsp; <strong>65.000 &euro;</strong> &nbsp;&middot;&nbsp; ab sofort',
    'quick_facts': [
        {'label': 'Gehalt', 'value': '65.000 EUR', 'detail': ''},
        {'label': 'Verfuegbar ab', 'value': 'Sofort', 'detail': ''},
        {'label': 'Anstellung', 'value': 'Vollzeit', 'detail': ''},
        {'label': 'Home-Office', 'value': '2 Tage / Woche', 'detail': ''},
        {'label': 'Pendel', 'value': 'Max. 45 Min.', 'detail': 'Auto + OEPNV'},
        {'label': 'Grossraumbuero', 'value': 'Kein Problem', 'detail': ''},
    ],
    'kurzprofil_items': [
        {'label': 'Wechselmotivation', 'text': 'Sucht neue Herausforderung mit mehr Verantwortung.'},
        {'label': 'Kernkompetenz', 'text': 'Erfahrener Bilanzbuchhalter mit Schwerpunkt HGB/IFRS.'},
    ],
    'erp_items': [
        {'name': 'SAP FI/CO', 'pct': 90, 'level_label': 'Experte'},
        {'name': 'DATEV', 'pct': 70, 'level_label': 'Fortgeschritten'},
    ],
    'languages': [
        {'name': 'Deutsch', 'pct': 100, 'level_label': 'Muttersprache'},
        {'name': 'Englisch', 'pct': 85, 'level_label': 'C1 - Fortgeschritten'},
    ],
    'skill_tags': [
        {'name': 'SAP FI/CO', 'highlight': True},
        {'name': 'DATEV', 'highlight': True},
        {'name': 'HGB', 'highlight': False},
        {'name': 'IFRS', 'highlight': False},
    ],
    'work_items': [
        {'title': 'Senior Bilanzbuchhalter', 'company': 'Test GmbH', 'date_range': '03/2021 - heute', 'note': '4 Jahre', 'bullets': ['Monats- und Jahresabschluesse nach HGB und IFRS', 'Konzernkonsolidierung fuer 12 Tochtergesellschaften']},
        {'title': 'Bilanzbuchhalter', 'company': 'Firma XY', 'date_range': '06/2017 - 02/2021', 'note': '', 'bullets': ['Eigenverantwortliche Erstellung der Monats- und Jahresabschluesse']},
    ],
    'edu_items': [
        {'name': 'Bilanzbuchhalter (IHK)', 'institution': 'IHK Hamburg', 'date': '2016'},
    ],
    'cert_items': [
        {'name': 'IFRS-Zertifikat', 'institution': 'Controller Akademie', 'year': '2023'},
    ],
    'it_tags': [], 'cv_languages': [], 'empfehlung_from': '', 'empfehlung_text': '',
    'consultant': {
        'name': 'Milad Hamdard',
        'title': 'Senior Consultant Finance & Engineering',
        'email': 'hamdard@sincirus.com',
        'phone': '0176 8000 47 41',
        'address': 'Ballindamm 3, 20095 Hamburg',
    },
    'logo_path': os.path.join(static_dir, 'images', 'sincirus_logo.png'),
    'logo_komplett_path': os.path.join(static_dir, 'images', 'sincirus_logo_komplett.png'),
    'logo_white_path': logo_data_uri(os.path.join(static_dir, 'images', 'sincirus_logo_komplett_white.png')),
    'logo_transparent_path': logo_data_uri(os.path.join(static_dir, 'images', 'sincirus_logo_komplett_transparent.png')),
    'photo_path': os.path.join(static_dir, 'images', 'milad_foto.jpg'),
    'font_dir': os.path.join(static_dir, 'fonts'),
}

html = template.render(**context)
# Browser-Preview: Grauer Hintergrund + Seitenschatten
html = html.replace(
    'background: var(--white);\\n    }',
    'background: #94a3b8; display: flex; flex-direction: column; align-items: center; padding: 40px 0; gap: 40px;\\n    }', 1)
with open('test_pdf_preview.html', 'w') as f:
    f.write(html)
print('Gespeichert: test_pdf_preview.html')
"
```

**Schritt 5: Beide Dateien nebeneinander im Browser vergleichen**
```
open preview_pdf_design.html test_pdf_preview.html
```
Pixel fuer Pixel vergleichen. JEDE Abweichung fixen.

**Schritt 6: Test-Datei loeschen, committen, pushen**
```bash
rm test_pdf_preview.html
git add app/templates/profile_sincirus_branded.html
git commit -m "Fix: PDF-Template [Beschreibung]"
git push origin main
```

**Schritt 7: Auf Railway warten (1-2 Min), PDF live testen**

---

## WEASYPRINT-BESONDERHEITEN

### Was WeasyPrint KANN:
- `rgba()`, `hsla()` Farben
- CSS Custom Properties (`var(--brand)`)
- `linear-gradient()`
- Flexbox (`display: flex`)
- CSS Grid (`display: grid`)
- `@font-face` mit lokalen TTF-Dateien
- `border-radius`, `box-shadow`
- `page-break-after`, `page-break-inside: avoid`
- `orphans`, `widows` (Seitenumbruch-Kontrolle)
- `::before`, `::after` Pseudo-Elemente

### Was WeasyPrint NICHT kann:
- `filter: brightness() invert()` — Deshalb nutzen wir ein weisses Logo-PNG statt CSS-Filter
- `@import url('https://fonts.googleapis.com/...')` — Deshalb `@font-face` mit lokalen Dateien
- `box-shadow` auf Seiten-Ebene wird im PDF nicht sichtbar (irrelevant)
- JavaScript — Template muss reines HTML+CSS sein
- Externe Bilder von URLs — Deshalb alle Logos als Base64 data-URI eingebettet

### Logo-Einbettung als Data-URI:
```python
# In profile_pdf_service.py — Methode _logo_as_data_uri()
# Konvertiert PNG zu data:image/png;base64,... fuer WeasyPrint-Kompatibilitaet
import base64
with open(path, "rb") as f:
    b64 = base64.b64encode(f.read()).decode()
return f"data:image/png;base64,{b64}"
```

### Font-Einbettung:
```css
/* Lokale TTF-Dateien statt Google Fonts URL */
@font-face {
  font-family: 'DM Sans';
  src: url('{{ font_dir }}/DMSans-Regular.ttf') format('truetype');
  font-weight: 100 900;
  font-style: normal;
}
```

---

## DATENFLUSS: VON DB ZUM PDF

```
Kandidat in DB
    |
    v
profile_pdf_service.py :: generate_profile_pdf(candidate_id)
    |
    ├── CandidateService.get_candidate() → Kandidat-Objekt
    |
    ├── _prepare_template_context() → Dict mit allen Template-Variablen:
    |     ├── _build_hero_title()       → "Bilanzbuchhalter (IHK)"
    |     ├── _build_hero_meta()        → "Vollzeit · 65.000 € · ab sofort"
    |     ├── _build_quick_facts()      → 6 Fact-Cards (Gehalt, Verfuegbar, ...)
    |     ├── _build_kurzprofil()       → Wechselmotivation + Kernkompetenz
    |     ├── _build_fallback_kurzprofil() → Falls kein Qualidaten: Position + Standort
    |     ├── _build_erp_items()        → ERP-Systeme mit Prozent-Balken
    |     ├── _build_languages()        → Sprachen mit Prozent-Balken
    |     ├── _build_skill_tags()       → Schluesselqualifikationen
    |     ├── _build_work_items()       → Berufserfahrung mit Bullets
    |     ├── _build_edu_items()        → Ausbildung
    |     └── _build_cert_items()       → Zertifikate
    |
    ├── Jinja2 rendert profile_sincirus_branded.html mit Context
    |
    └── WeasyPrint: HTML → PDF (in Executor fuer async)
         |
         v
    PDF bytes → Response oder Base64 fuer E-Mail-Anhang
```

### Fallback-Logik:
- **Kurzprofil:** Wenn `candidate_notes` und `v2_current_role_summary` leer → Fallback zu `current_position` + `city`
- **Quick Facts:** Wenn keine Qualidaten → Fallback zu Position + Standort + Erfahrung + Level
- **ERP-Balken:** Wenn kein `proficiency` in `v2_structured_skills` → Default 50%
- **Sprach-Balken:** Keyword-Suche in Level-String (z.B. "mutter" → 100%, "flie" → 95%)

---

## DESIGN-REFERENZ (CSS Custom Properties)

```css
--brand: #0b89bd;          /* Hauptfarbe */
--brand-dark: #087099;     /* Dunkler */
--brand-light: #0d9dd6;    /* Heller */
--brand-glow: #10b5f0;     /* Leuchtend */
--navy: #0f172a;           /* Sehr dunkel (Ueberschriften) */
--cyan: #0d9dd6;           /* Akzent (Fact-Card Rand) */
--bg-subtle: #F8FAFC;      /* Hintergrund Cards */
--border: #E2E8F0;         /* Raender */
--text: #0f172a;           /* Haupttext */
--text-sec: #334155;       /* Sekundaertext */
--text-muted: #64748b;     /* Gedaempft */
--text-subtle: #94a3b8;    /* Subtil */

--gradient-header: linear-gradient(145deg, #076a94 0%, #0b89bd 40%, #0d9dd6 75%, #10b5f0 100%);
--gradient-bar: linear-gradient(90deg, #0b89bd, #10b5f0);
```

---

## GOLDEN RULES

1. **`preview_pdf_design.html` ist die EINZIGE Wahrheit** — Nicht das PDF, nicht die Erinnerung, nur dieses HTML.
2. **NIEMALS CSS abschneiden** — Jede Klasse braucht `{` und `}`.
3. **NIEMALS rgba() durch Hex ersetzen** — WeasyPrint kann rgba.
4. **IMMER nach Aenderung eine Test-HTML generieren** — Nicht blind deployen.
5. **IMMER beide Dateien nebeneinander vergleichen** — Pixel fuer Pixel.
6. **Foto-Groesse: 82px** (Milads Wunsch: 20% groesser als Design-Vorlage).
7. **Logos werden als Base64 data-URI eingebettet** — Nicht als Dateipfad.
8. **Fonts werden lokal per @font-face geladen** — Nicht per Google Fonts URL.
