"""
Gender-Inferenz anhand deutscher Vornamen.

Bestimmt 'Herr' oder 'Frau' basierend auf gaengigen deutschen Vornamen.
Unbekannte oder geschlechtsambige Namen geben None zurueck.
"""

# ~200 gaengige deutsche Vornamen → Anrede
# Geschlechtsambige Namen (Kim, Toni, Sascha) sind bewusst NICHT enthalten
GERMAN_FIRST_NAMES: dict[str, str] = {
    # ── Maennlich ──────────────────────────────────────────
    "Thomas": "Herr", "Michael": "Herr", "Stefan": "Herr", "Andreas": "Herr",
    "Christian": "Herr", "Martin": "Herr", "Alexander": "Herr", "Daniel": "Herr",
    "Markus": "Herr", "Marcus": "Herr", "Peter": "Herr", "Frank": "Herr",
    "Wolfgang": "Herr", "Klaus": "Herr", "Claus": "Herr", "Tobias": "Herr",
    "Matthias": "Herr", "Mathias": "Herr", "Bernd": "Herr", "Ralf": "Herr",
    "Ralph": "Herr", "Uwe": "Herr", "Sven": "Herr", "Lars": "Herr",
    "Tim": "Herr", "Jan": "Herr", "Florian": "Herr", "Sebastian": "Herr",
    "Philipp": "Herr", "Philip": "Herr", "Oliver": "Herr", "Patrick": "Herr",
    "Marco": "Herr", "Marcel": "Herr", "Dennis": "Herr", "Denis": "Herr",
    "Manuel": "Herr", "Benjamin": "Herr", "Felix": "Herr", "Lukas": "Herr",
    "Lucas": "Herr", "Maximilian": "Herr", "Max": "Herr", "David": "Herr",
    "Robert": "Herr", "Harald": "Herr", "Dirk": "Herr", "Volker": "Herr",
    "Holger": "Herr", "Werner": "Herr", "Dieter": "Herr", "Rainer": "Herr",
    "Heinz": "Herr", "Helmut": "Herr", "Hermann": "Herr", "Friedrich": "Herr",
    "Hans": "Herr", "Kurt": "Herr", "Walter": "Herr", "Karl": "Herr",
    "Gerhard": "Herr", "Manfred": "Herr", "Horst": "Herr", "Rudolf": "Herr",
    "Otto": "Herr", "Norbert": "Herr", "Armin": "Herr", "Erwin": "Herr",
    "Gerd": "Herr", "Georg": "Herr", "Joachim": "Herr", "Ernst": "Herr",
    "Erich": "Herr", "Bernhard": "Herr", "Wilhelm": "Herr", "Heinrich": "Herr",
    "Jochen": "Herr", "Carsten": "Herr", "Karsten": "Herr", "Thorsten": "Herr",
    "Torsten": "Herr", "Jens": "Herr", "Kai": "Herr", "Kay": "Herr",
    "Ingo": "Herr", "Olaf": "Herr", "Axel": "Herr", "Detlef": "Herr",
    "Christoph": "Herr", "Christopher": "Herr", "Dominik": "Herr",
    "Steffen": "Herr", "Stephan": "Herr", "Kevin": "Herr", "Nico": "Herr",
    "Nils": "Herr", "Paul": "Herr", "Leon": "Herr", "Jonas": "Herr",
    "Elias": "Herr", "Noah": "Herr", "Liam": "Herr", "Finn": "Herr",
    "Moritz": "Herr", "Simon": "Herr", "Fabian": "Herr", "Henrik": "Herr",
    "Henning": "Herr", "Clemens": "Herr", "Benedikt": "Herr", "Anton": "Herr",
    "Jakob": "Herr", "Bastian": "Herr", "Malte": "Herr", "Marvin": "Herr",
    "Erik": "Herr", "Eric": "Herr", "Niklas": "Herr", "Robin": "Herr",
    "Sascha": "Herr",  # In Deutschland ueblicherweise maennlich
    # Umlaut-Varianten
    "Juergen": "Herr", "Jürgen": "Herr", "Guenter": "Herr", "Günter": "Herr",
    "Guenther": "Herr", "Günther": "Herr", "Joerg": "Herr", "Jörg": "Herr",
    "Ruediger": "Herr", "Rüdiger": "Herr", "Ulf": "Herr",

    # ── Weiblich ───────────────────────────────────────────
    "Sabine": "Frau", "Petra": "Frau", "Claudia": "Frau",
    "Susanne": "Frau", "Monika": "Frau", "Stefanie": "Frau", "Stephanie": "Frau",
    "Christina": "Frau", "Christine": "Frau", "Katrin": "Frau", "Kathrin": "Frau",
    "Birgit": "Frau", "Nicole": "Frau", "Sandra": "Frau", "Julia": "Frau",
    "Anna": "Frau", "Karin": "Frau", "Heike": "Frau", "Barbara": "Frau",
    "Martina": "Frau", "Gabriele": "Frau", "Ute": "Frau", "Anke": "Frau",
    "Silke": "Frau", "Anja": "Frau", "Tanja": "Frau", "Marion": "Frau",
    "Simone": "Frau", "Jutta": "Frau", "Bettina": "Frau", "Cornelia": "Frau",
    "Maria": "Frau", "Ursula": "Frau", "Renate": "Frau", "Helga": "Frau",
    "Gisela": "Frau", "Ingrid": "Frau", "Dagmar": "Frau", "Sonja": "Frau",
    "Doris": "Frau", "Beate": "Frau", "Christiane": "Frau", "Angelika": "Frau",
    "Gudrun": "Frau", "Hildegard": "Frau", "Elke": "Frau", "Katharina": "Frau",
    "Ines": "Frau", "Daniela": "Frau", "Melanie": "Frau", "Nadine": "Frau",
    "Manuela": "Frau", "Jasmin": "Frau", "Lena": "Frau", "Sarah": "Frau",
    "Sara": "Frau", "Laura": "Frau", "Lisa": "Frau", "Sophie": "Frau",
    "Sophia": "Frau", "Alexandra": "Frau", "Eva": "Frau",
    "Erika": "Frau", "Ruth": "Frau", "Irmgard": "Frau", "Elisabeth": "Frau",
    "Gertrud": "Frau", "Johanna": "Frau", "Emma": "Frau", "Hanna": "Frau",
    "Hannah": "Frau", "Frieda": "Frau", "Maren": "Frau", "Svenja": "Frau",
    "Janina": "Frau", "Wiebke": "Frau", "Frauke": "Frau", "Britta": "Frau",
    "Maike": "Frau", "Meike": "Frau", "Kirsten": "Frau", "Kerstin": "Frau",
    "Astrid": "Frau", "Sigrid": "Frau", "Ilse": "Frau", "Hannelore": "Frau",
    "Margit": "Frau", "Margret": "Frau", "Margarete": "Frau", "Christa": "Frau",
    "Edith": "Frau", "Elfriede": "Frau", "Waltraud": "Frau", "Roswitha": "Frau",
    "Gabriela": "Frau", "Verena": "Frau", "Ramona": "Frau", "Carina": "Frau",
    "Karina": "Frau", "Vanessa": "Frau", "Jessica": "Frau", "Jennifer": "Frau",
    "Steffi": "Frau", "Annika": "Frau", "Miriam": "Frau", "Franziska": "Frau",
    "Theresa": "Frau", "Teresa": "Frau", "Frederike": "Frau", "Friederike": "Frau",
    "Annerose": "Frau", "Annette": "Frau", "Anne": "Frau", "Antje": "Frau",
    "Mia": "Frau", "Emilia": "Frau", "Marie": "Frau", "Leonie": "Frau",
    "Amelie": "Frau", "Nele": "Frau", "Ida": "Frau", "Greta": "Frau",
    "Andrea": "Frau",  # In Deutschland ueblicherweise weiblich
}


def infer_salutation(first_name: str | None) -> str | None:
    """Leitet 'Herr' oder 'Frau' aus einem deutschen Vornamen ab.

    Returns None wenn der Name unbekannt oder nicht zuordenbar ist.
    Ueberschreibt NIEMALS eine bestehende Anrede.
    """
    if not first_name or not first_name.strip():
        return None

    # Ersten Vornamen nehmen (bei "Hans-Peter" → "Hans", bei "Anna Maria" → "Anna")
    clean = first_name.strip().split()[0].split("-")[0]
    # Capitalize fuer konsistentes Matching
    normalized = clean.capitalize()

    return GERMAN_FIRST_NAMES.get(normalized)


def apply_salutation_if_missing(
    salutation: str | None, first_name: str | None
) -> str | None:
    """Gibt bestehende Anrede zurueck oder leitet sie aus dem Vornamen ab.

    Regel: Bestehende Anrede wird NIEMALS ueberschrieben.
    Nur wenn salutation None/leer ist, wird aus first_name abgeleitet.
    """
    if salutation and salutation.strip():
        return salutation
    return infer_salutation(first_name)
