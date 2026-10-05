#!/usr/bin/env python3
"""
rft_stats.py — Turnier-Statistiken für League-of-Legends-Profispieler von rft.gg

Nimmt einen Spielernamen entgegen, sucht dessen rft.gg-Profil, sammelt für
eine fest definierte Auswahl an Turnieren (siehe TOURNAMENT_RULES unten) pro
Spiel die Werte Sieg/Niederlage, RFT-1.0-Rating, KDA und DPM und berechnet
daraus - pro Turnier-Kategorie UND als Gesamtwert über alle gewählten
Turniere zusammen (automatisch "nach Spielen gewichtet", weil er aus allen
Einzelspielen berechnet wird statt aus dem Mittel der Kategorie-Mittelwerte):

  - Gesamtzahl der Spiele (Wins + Losses)
  - Winrate
  - durchschnittliches RFT-1.0-Rating
  - durchschnittliche KDA
  - durchschnittliche DPM

------------------------------------------------------------------------
NUTZUNG IM TERMINAL
------------------------------------------------------------------------
    pip install requests beautifulsoup4
    python3 rft_stats.py "Isles"
    python3 rft_stats.py "Isles" --url https://rft.gg/player/1110-isles
    python3 rft_stats.py "Isles" --debug

------------------------------------------------------------------------
NUTZUNG IN JUPYTER LAB
------------------------------------------------------------------------
Einfach in einer Zelle:

    from rft_stats import run
    per_category, overall, games = run("Isles")

`run()` druckt den Bericht direkt aus und gibt zusätzlich die Rohdaten
zurück (per_category als dict, overall als dict, games als Liste von
GameResult-Objekten), falls du selbst weiterrechnen/plotten willst, z.B.:

    from rft_stats import run, to_dataframe
    per_category, overall, games = run("Isles")
    df = to_dataframe(games)   # braucht `pip install pandas`

Das Skript führt beim reinen Import (bzw. `%run` unter Jupyter) NICHTS
automatisch aus — es reagiert nur auf run(...) oder, im Terminal, auf echte
CLI-Argumente. Dadurch stört es nicht, dass Jupyter beim Start eigene
Kommandozeilen-Argumente (Kernel-Connection-File etc.) mitgibt, was sonst
zu einem argparse-Fehler führen würde.

------------------------------------------------------------------------
WICHTIGER HINWEIS ZUR GENAUIGKEIT (bitte lesen)
------------------------------------------------------------------------
rft.gg bietet keine öffentliche/dokumentierte API. Dieses Skript liest
daher die normalen HTML-Seiten aus (serverseitig gerendert, d.h. requests +
BeautifulSoup reichen, kein Selenium/Playwright nötig).

Die genaue Feinstruktur der /matches-Unterseite und der einzelnen
Match-Seiten konnte ich beim Schreiben nur eingeschränkt einsehen (mein
Zugriffstool lieferte mir die Seite nur als vereinfachten, "flach
geklopften" Text ohne CSS-Klassen). Deshalb suchen die Parsing-Funktionen
bewusst nach *Textmustern* ("KDA"/"DPM"/"RFT" jeweils direkt neben einer
Zahl) statt nach starren CSS-Selektoren — robuster gegen Layout-Details,
aber keine Garantie. Ebenso ist unklar, ob rft.gg die LCK-Turniernamen
exakt so schreibt wie unten konfiguriert (z.B. Einzahl/Mehrzahl bei
"Regional Finals") — kurz auf https://rft.gg/events gegenchecken.

Lauf das Skript einmal mit debug=True (bzw. --debug im Terminal). Für jedes
Match, aus dem sich Werte nicht extrahieren ließen, wird die HTML-Rohseite
unter debug_dumps/ gespeichert. Ein Blick hinein (oder "Element
untersuchen" im Browser auf der echten Seite) zeigt dir die tatsächliche
Struktur — passe dann _extract_labeled_number() / _find_player_block() an,
oder schick mir einen Ausschnitt.
------------------------------------------------------------------------
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://rft.gg"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}
REQUEST_DELAY = 0.6  # Sekunden zwischen Requests, um den Server nicht zu hämmern
DEBUG_DIR = Path("debug_dumps")


# ---------------------------------------------------------------------------
# Turnier-Konfiguration
# ---------------------------------------------------------------------------
# Jeder Eintrag: (Name, ab Jahr (inkl.), bis Jahr (inkl., None = offen)).
#
# Reihenfolge ist wichtig: spezifischere Namen stehen weiter oben, damit z.B.
# "LCK Road to MSI" nicht versehentlich unter "MSI" einsortiert wird (beide
# enthalten den Text "MSI"). Ein Match wird der ERSTEN passenden Regel
# zugeordnet.
# ---------------------------------------------------------------------------
TOURNAMENT_RULES: list[tuple[str, int, Optional[int]]] = [
    ("LCK Regional Finals", 2022, 2024),
    ("LCK Road to MSI", 2025, None),
    ("LCK Spring", 2022, 2024),
    ("LCK Summer", 2022, 2024),
    ("LCK Cup", 2025, None),
    ("LCK Season", 2025, None),
    ("First Stand", 2022, None),
    ("World Championship", 2022, None),
    ("MSI", 2022, None),
]


@dataclass
class GameResult:
    tournament: str                # roher Turniername, wie auf der Matchseite gefunden
    category: Optional[str]        # zugeordnete Kategorie aus TOURNAMENT_RULES (None = nicht relevant)
    year: Optional[int]
    win: Optional[bool]
    rft: Optional[float]
    kda: Optional[float]
    dpm: Optional[float]
    match_url: str


def get(url: str) -> BeautifulSoup:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    time.sleep(REQUEST_DELAY)
    return BeautifulSoup(resp.text, "html.parser")


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def _dump(html: str, name: str) -> None:
    DEBUG_DIR.mkdir(exist_ok=True)
    path = DEBUG_DIR / f"{slugify(name)}.html"
    path.write_text(html, encoding="utf-8")


def _normalize_tournament(text: str) -> str:
    """Entfernt Jahreszahlen und normalisiert Whitespace, für robusten Abgleich
    unabhängig davon, ob die Jahreszahl vor/nach dem Turniernamen steht
    (rft.gg schreibt z.B. "LCK 2023 Spring", nicht "LCK Spring 2023")."""
    text = re.sub(r"\b20\d{2}\b", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


def classify_tournament(
    tournament_text: str,
    rules: Optional[list[tuple[str, int, Optional[int]]]] = None,
) -> tuple[Optional[str], Optional[int]]:
    """
    Ordnet einen auf rft.gg gefundenen Turniernamen (z.B. "LCK 2023 Spring")
    einer der konfigurierten TOURNAMENT_RULES zu.

    Rückgabe: (Kategorie-Name oder None, erkanntes Jahr oder None).
    Kategorie ist None, wenn kein konfiguriertes Turnier passt, ODER wenn es
    zwar passt aber außerhalb des erlaubten Jahresbereichs liegt, ODER wenn
    der Name zwar passt aber gar kein Jahr im Text erkannt wurde (dann lässt
    sich der Jahresbereich nicht prüfen -> sicherheitshalber ausgeschlossen).
    """
    rules = rules if rules is not None else TOURNAMENT_RULES
    year_match = re.search(r"\b(20\d{2})\b", tournament_text)
    year = int(year_match.group(1)) if year_match else None
    normalized = _normalize_tournament(tournament_text)

    for name, min_year, max_year in rules:
        if _normalize_tournament(name) in normalized:
            if year is None:
                return None, None
            if year < min_year or (max_year is not None and year > max_year):
                return None, year
            return name, year
    return None, year


# ---------------------------------------------------------------------------
# Schritt 1: Spielername -> rft.gg-Profil-URL
# ---------------------------------------------------------------------------
def _collect_player_urls_from_sitemap() -> list[str]:
    """
    rft.gg hat keine dokumentierte Such-API; die Live-Suche im Frontend läuft
    clientseitig per JavaScript. Als robuster Weg ohne Browser-Automation wird
    stattdessen die sitemap.xml durchsucht (bei Next.js-Seiten sehr üblich),
    die i.d.R. alle /player/<id>-<slug>-URLs auflistet.
    """
    urls: list[str] = []
    try:
        r = requests.get(f"{BASE_URL}/sitemap.xml", headers=HEADERS, timeout=15)
    except requests.RequestException:
        return urls
    if not r.ok:
        return urls

    locs = re.findall(r"<loc>(.*?)</loc>", r.text)

    sub_sitemaps = [l for l in locs if l.endswith(".xml")]
    for sm in sub_sitemaps:
        if "player" in sm.lower():
            try:
                r2 = requests.get(sm, headers=HEADERS, timeout=15)
                if r2.ok:
                    locs.extend(re.findall(r"<loc>(.*?)</loc>", r2.text))
            except requests.RequestException:
                continue

    return [l for l in locs if "/player/" in l]


def find_player_url(name: str) -> str:
    slug = slugify(name)
    player_urls = _collect_player_urls_from_sitemap()

    if not player_urls:
        raise RuntimeError(
            "Konnte keine Spieler-URLs über die sitemap.xml finden (evtl. gibt "
            "es dort keine, oder der Pfad heißt anders). Bitte die Profil-URL "
            f"manuell übergeben, z.B. run({name!r}, url='https://rft.gg/player/1110-isles')"
        )

    exact = [u for u in player_urls if u.rstrip("/").split("/")[-1].endswith(f"-{slug}")]
    if exact:
        return exact[0]

    slug_to_url = {u.rstrip("/").split("/")[-1]: u for u in player_urls}
    best = difflib.get_close_matches(slug, slug_to_url.keys(), n=1, cutoff=0.5)
    if best:
        return slug_to_url[best[0]]

    raise RuntimeError(
        f"Kein rft.gg-Profil für '{name}' gefunden. Bitte die Profil-URL manuell "
        f"übergeben, z.B. run({name!r}, url='https://rft.gg/player/1110-isles')"
    )


# ---------------------------------------------------------------------------
# Schritt 2: Für den Spieler alle Match-Links einsammeln
# ---------------------------------------------------------------------------
def collect_match_urls(player_url: str, debug: bool = False) -> list[str]:
    matches_url = player_url.rstrip("/") + "/matches"
    match_links: set[str] = set()

    page = 1
    while True:
        url = matches_url if page == 1 else f"{matches_url}?page={page}"
        try:
            soup = get(url)
        except requests.RequestException as e:
            print(f"Warnung: {url} konnte nicht geladen werden ({e})", file=sys.stderr)
            break

        if debug:
            _dump(str(soup), f"matches_page_{page}")

        links = {
            (href if href.startswith("http") else BASE_URL + href)
            for a in soup.find_all("a", href=True)
            for href in [a["href"]]
            if re.match(r"^(/match/|https://rft\.gg/match/)", href)
        }
        new_links = links - match_links
        if not new_links:
            break
        match_links |= new_links

        has_next = soup.find("a", href=re.compile(rf"page={page + 1}\b"))
        if not has_next:
            break
        page += 1
        if page > 50:  # Sicherheitsnetz gegen Endlosschleifen
            break

    return sorted(match_links)


# ---------------------------------------------------------------------------
# Schritt 3: Aus jeder Matchseite die Werte für den Spieler herausziehen
# ---------------------------------------------------------------------------
NUMBER = r"(\d+(?:\.\d+)?)"


def _extract_labeled_number(text: str, label: str) -> Optional[float]:
    # Zuerst "Zahl direkt vor dem Label" prüfen (z.B. "77RFT 1.0" -> 77;
    # sonst würde die "1.0" aus dem Label-Namen "RFT 1.0" selbst
    # fälschlich als Wert erkannt).
    m = re.search(rf"{NUMBER}\D{{0,5}}{label}", text, re.I)
    if m:
        return float(m.group(1))
    m = re.search(rf"{label}\D{{0,5}}{NUMBER}", text, re.I)
    if m:
        return float(m.group(1))
    return None


def _find_player_block(soup: BeautifulSoup, player_name: str, max_levels: int = 6):
    """
    Findet das umgebende Element (Zeile/Karte) für den Spieler auf einer
    Matchseite. Klettert je Namensvorkommen schrittweise nach oben, bis
    KDA/DPM/RFT in der Nähe gefunden werden (nicht nach Textlänge — das würde
    sonst leicht die Zeile des Nachbarspielers mit erfassen). Von mehreren
    Namensvorkommen (z.B. einer News-Erwähnung ohne Statistiken) wird das mit
    den meisten gefundenen Statistiken gewählt.
    """
    name_nodes = soup.find_all(string=re.compile(rf"^\s*{re.escape(player_name)}\s*$", re.I))
    if not name_nodes:
        return None

    candidates = []
    for name_node in name_nodes:
        node = name_node.parent
        for _ in range(max_levels):
            if node is None:
                break
            text = node.get_text(" ", strip=True)
            stat_count = sum(
                _extract_labeled_number(text, lbl) is not None
                for lbl in ("KDA", "DPM", "RFT")
            )
            if stat_count > 0:
                candidates.append((stat_count, len(text), node))
                break
            if node.parent is None:
                break
            node = node.parent

    if not candidates:
        return name_nodes[0].parent

    candidates.sort(key=lambda c: (-c[0], c[1]))
    return candidates[0][2]


def _extract_win_loss(text: str) -> Optional[bool]:
    if re.search(r"\bwin\b|\bvictory\b|\bsieg\b", text, re.I):
        return True
    if re.search(r"\bloss\b|\bdefeat\b|\bniederlage\b", text, re.I):
        return False
    w = re.search(r"\bW\b", text)
    l = re.search(r"\bL\b", text)
    if w and not l:
        return True
    if l and not w:
        return False
    return None


def parse_match_for_player(
    match_url: str,
    player_name: str,
    debug: bool = False,
    rules: Optional[list[tuple[str, int, Optional[int]]]] = None,
) -> Optional[GameResult]:
    try:
        soup = get(match_url)
    except requests.RequestException as e:
        print(f"Warnung: {match_url} konnte nicht geladen werden ({e})", file=sys.stderr)
        return None

    if debug:
        _dump(str(soup), f"match_{match_url.rsplit('/', 1)[-1]}")

    tournament_text = None
    event_link = soup.find("a", href=re.compile(r"/event/"))
    if event_link:
        tournament_text = event_link.get_text(strip=True)
    elif soup.title and soup.title.string:
        tournament_text = soup.title.string.split("|")[0].strip()
    tournament_text = tournament_text or "Unbekannt"

    category, year = classify_tournament(tournament_text, rules=rules)

    block = _find_player_block(soup, player_name)
    if block is None:
        return None
    text = block.get_text(" ", strip=True)

    return GameResult(
        tournament=tournament_text,
        category=category,
        year=year,
        win=_extract_win_loss(text),
        rft=_extract_labeled_number(text, "RFT"),
        kda=_extract_labeled_number(text, "KDA"),
        dpm=_extract_labeled_number(text, "DPM"),
        match_url=match_url,
    )


# ---------------------------------------------------------------------------
# Schritt 4: Auswertung
# ---------------------------------------------------------------------------
def summarize(games: list[GameResult]) -> dict:
    wins = sum(1 for g in games if g.win is True)
    losses = sum(1 for g in games if g.win is False)
    total = wins + losses

    def avg(values):
        values = [v for v in values if v is not None]
        return sum(values) / len(values) if values else None

    return {
        "games": total,
        "wins": wins,
        "losses": losses,
        "winrate": (wins / total) if total else None,
        "avg_rft": avg(g.rft for g in games),
        "avg_kda": avg(g.kda for g in games),
        "avg_dpm": avg(g.dpm for g in games),
    }


def fmt(value, digits=2, suffix="") -> str:
    return f"{value:.{digits}f}{suffix}" if value is not None else "?"


def print_report(player_name: str, per_category: dict, overall: dict) -> None:
    print(f"\n=== {player_name} — Turnier-Statistiken (rft.gg) ===\n")
    for category, stats in per_category.items():
        print(f"[{category}]")
        print(f"  Games:      {stats['games']}  ({stats['wins']}W {stats['losses']}L)")
        print(f"  Winrate:    {fmt((stats['winrate'] or 0) * 100, 1, '%')}")
        print(f"  Ø RFT 1.0:  {fmt(stats['avg_rft'])}")
        print(f"  Ø KDA:      {fmt(stats['avg_kda'])}")
        print(f"  Ø DPM:      {fmt(stats['avg_dpm'], 0)}")
        print()

    print("[Gesamt über alle konfigurierten Turniere, gewichtet nach Spielen]")
    print(f"  Games:      {overall['games']}  ({overall['wins']}W {overall['losses']}L)")
    print(f"  Winrate:    {fmt((overall['winrate'] or 0) * 100, 1, '%')}")
    print(f"  Ø RFT 1.0:  {fmt(overall['avg_rft'])}")
    print(f"  Ø KDA:      {fmt(overall['avg_kda'])}")
    print(f"  Ø DPM:      {fmt(overall['avg_dpm'], 0)}")


def to_dataframe(games: list[GameResult]):
    """Optionaler Komfort für Jupyter: Liste von GameResult -> pandas.DataFrame."""
    try:
        import pandas as pd
    except ImportError as e:
        raise ImportError(
            "pandas ist nicht installiert. `pip install pandas` ausführen, "
            "oder ohne DataFrame direkt mit der games-Liste weiterarbeiten."
        ) from e
    return pd.DataFrame([g.__dict__ for g in games])


# ---------------------------------------------------------------------------
# Öffentliche Haupt-Funktion — das hier in Jupyter aufrufen
# ---------------------------------------------------------------------------
def run(
    player: str,
    url: Optional[str] = None,
    debug: bool = False,
    tournament_rules: Optional[list[tuple[str, int, Optional[int]]]] = None,
):
    """
    Holt und berechnet die Turnier-Statistiken für `player` und druckt einen
    Bericht aus. Rückgabe: (per_category: dict, overall: dict,
    games: list[GameResult]) für eigene Weiterverarbeitung, z.B. mit pandas
    über to_dataframe(games).

    tournament_rules erlaubt es, für einen einzelnen Aufruf eine andere
    Turnierauswahl als TOURNAMENT_RULES zu verwenden, ohne die Datei zu
    ändern, z.B.:

        run("Isles", tournament_rules=[("MSI", 2022, None)])
    """
    rules = tournament_rules if tournament_rules is not None else TOURNAMENT_RULES

    resolved_url = url or find_player_url(player)
    print(f"Profil gefunden: {resolved_url}")

    print("Sammle Match-Links …")
    match_urls = collect_match_urls(resolved_url, debug=debug)
    print(f"{len(match_urls)} Matches gefunden. Werte werden ausgelesen …")

    all_games: list[GameResult] = []
    for i, match_url in enumerate(match_urls, 1):
        print(f"  ({i}/{len(match_urls)}) {match_url}", end="\r")
        game = parse_match_for_player(match_url, player, debug=debug, rules=rules)
        if game:
            all_games.append(game)
    print()

    selected = [g for g in all_games if g.category is not None]

    if not selected:
        found = sorted(set(g.tournament for g in all_games))
        print(
            "\nKeine Spiele aus den konfigurierten Turnieren/Jahren gefunden.\n"
            f"Auf den Matchseiten gefundene Turniernamen (Beispiele): {found[:10]}\n"
            "-> TOURNAMENT_RULES oben ggf. an die exakte Schreibweise/Jahre anpassen "
            "(siehe https://rft.gg/events)."
        )
        return {}, summarize([]), all_games

    per_category = {}
    for category in sorted({g.category for g in selected}):
        per_category[category] = summarize([g for g in selected if g.category == category])

    overall = summarize(selected)
    print_report(player, per_category, overall)
    return per_category, overall, all_games


# ---------------------------------------------------------------------------
# CLI für Terminal-Nutzung (unter Jupyter automatisch übersprungen)
# ---------------------------------------------------------------------------
def _running_under_jupyter() -> bool:
    return bool(sys.argv) and "ipykernel_launcher" in sys.argv[0]


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="rft.gg Turnier-Statistiken")
    parser.add_argument("player", help="Spielername, z.B. Isles")
    parser.add_argument("--url", help="rft.gg-Profil-URL, falls Auto-Suche fehlschlägt")
    parser.add_argument("--debug", action="store_true", help="HTML-Dumps in debug_dumps/ speichern")
    args = parser.parse_args(argv)

    try:
        run(args.player, url=args.url, debug=args.debug)
    except RuntimeError as e:
        print(f"Fehler: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    if _running_under_jupyter():
        print(
            "rft_stats.py wurde unter Jupyter geladen — es wird nichts automatisch "
            "ausgeführt (Jupyters eigene Start-Argumente würden sonst mit den "
            "Skript-Argumenten kollidieren). Nutze stattdessen in einer Zelle:\n\n"
            "    from rft_stats import run\n"
            "    per_category, overall, games = run('Spielername')\n"
        )
    else:
        main()
