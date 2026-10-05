#!/usr/bin/env python3
"""
rft_scraper.py — Historische LCK-Toplaner-Performance-Daten von rft.gg

Baut auf der Next.js-__next_f-Entdeckung auf (Performance-Daten stecken als
JSON in <script>self.__next_f.push([...])</script>-Blöcken, nicht in
normalen HTML-Tabellen) und verbindet sie mit den bereits gefundenen
Event-IDs, um Games direkt (ohne Fuzzy-Text-Matching!) den gewünschten
Turnieren zuzuordnen.

------------------------------------------------------------------------
WORKFLOW
------------------------------------------------------------------------
1. Für jeden Spieler in PLAYER_IDS und jedes Jahr in YEARS:
   https://rft.gg/player/<id>-<slug>?year=<jahr> abrufen, das
   "performances"-Array aus dem __next_f-Block extrahieren.
2. Für jede in den Performances vorkommende matchId (einmal pro Match,
   nicht pro Game!) die Match-Seite abrufen und die eventId auslesen.
   Ergebnis wird in match_event_cache.json zwischengespeichert, damit
   ein Match nicht mehrfach abgerufen wird (verschiedene Spieler/Jahre
   teilen sich oft dieselben Matches).
3. eventId -> Kategorie über EVENT_ID_TO_CATEGORY (aus deiner Recherche)
   auflösen; Games ohne bekannte/gewünschte eventId werden verworfen.
4. Alles in rft_games.csv schreiben (eine Zeile pro Game).

------------------------------------------------------------------------
WAS DU NOCH EINTRAGEN MUSST
------------------------------------------------------------------------
PLAYER_IDS: Bisher ist nur Zeus (16) bekannt. Für die anderen 14 Namen
fehlt die rft.gg-ID noch. Ich habe keinen zuverlässigen automatischen Weg
gefunden, Namen -> ID aufzulösen (keine sitemap.xml, rft.gg ist bei
Suchmaschinen kaum indexiert, die UI-Suche läuft clientseitig). Am
schnellsten geht es manuell, genau wie bei Zeus:
  1. https://rft.gg im Browser öffnen
  2. Spieler über die Suche finden
  3. ID aus der URL ablesen (z.B. https://rft.gg/player/16-zeus -> 16)
Bei 14 Namen ist das ein paar Minuten Aufwand, aber zuverlässig.

Falls der Slug (der Namensteil in der URL) doch nicht einfach
"name.lower()" ist, in PLAYER_SLUGS überschreiben.
------------------------------------------------------------------------
"""

from __future__ import annotations

import csv
import json
import os
import re
import time
from collections import OrderedDict
from typing import Optional

import requests

BASE_URL = "https://rft.gg"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; rft-scraper/2.0)"}
REQUEST_DELAY = 1.5  # Sekunden zwischen Requests — 0.25s hat zu HTTP-429-Rate-Limiting geführt
CACHE_FILE = "match_event_cache.json"
DEBUG_DIR = "debug_dumps"

# ---------------------------------------------------------------------------
# Bekannte Event-IDs -> Kategorie (aus deiner Recherche). Nur Turniere, die
# hier eingetragen sind, landen später in rft_games.csv — alles andere
# (andere Ligen, nicht gelistete Splits) wird automatisch verworfen.
# ---------------------------------------------------------------------------
EVENT_ID_TO_CATEGORY: dict[int, str] = {
    1856: "LCK Season", 74: "LCK Cup",
    1538: "LCK Season", 1534: "LCK Cup", 1536: "LCK Road to MSI", 1587: "First Stand",
    1426: "LCK Spring", 1427: "LCK Summer", 1428: "LCK Regional Finals",
    1462: "MSI", 1465: "World Championship",
    1264: "LCK Spring", 1265: "LCK Summer", 1317: "MSI", 1332: "World Championship",
    1125: "LCK Spring", 1126: "LCK Summer", 1195: "MSI", 1200: "World Championship",
}

# ---------------------------------------------------------------------------
# Spieler-IDs — siehe Hinweis oben. None = wird beim Lauf übersprungen
# (mit Warnung), damit das Skript trotzdem für die bereits bekannten
# Spieler durchläuft.
# ---------------------------------------------------------------------------
PLAYER_IDS: dict[str, Optional[int]] = {
    "Zeus": 16,
    "Doran": None,
    "Kiin": None,
    "Kingen": None,
    "DuDu": None,
    "Clear": None,
    "PerfecT": None,
    "Siwoo": None,
    "Rich": None,
    "Casting": None,
    "Morgan": None,
    "Rascal": None,
    "Canna": None,
    "DnDn": None,
    "Burdol": None,
}

# Nur nötig, falls der URL-Slug NICHT einfach name.lower() ist.
PLAYER_SLUGS: dict[str, str] = {}

YEARS = range(2022, 2027)  # 2022–2026 inklusive

GAME_FIELDS = [
    "player", "year", "gameId", "matchId", "gameDate", "opponentTeamName",
    "rftRating", "kda", "dpm", "gpm", "csm", "vision", "win", "role",
    "eventId", "category",
]


# ---------------------------------------------------------------------------
# Next.js __next_f-Extraktion
# ---------------------------------------------------------------------------
def extract_json_array(html: str, key: str) -> Optional[list]:
    """
    Findet '\\"<key>\\":[' im ROHEN (noch JS-escapten) HTML eines
    __next_f-Blocks, klettert klammerbalanciert (escape-/string-bewusst)
    zum Ende des Arrays und liefert es sauber dekodiert als Python-Liste.

    Gibt None zurück, wenn der Key nicht gefunden wird oder das Array
    nicht sauber terminiert (z.B. weil die Seite anders aufgebaut ist als
    erwartet) — Aufrufer sollten das behandeln, statt blind zu vertrauen.
    """
    marker = f'\\"{key}\\":['
    pos = html.find(marker)
    if pos == -1:
        return None

    start = pos + len(marker) - 1  # zeigt auf das öffnende '['
    depth = 0
    in_string = False
    escape = False
    end = None

    for i in range(start, len(html)):
        c = html[i]
        if escape:
            escape = False
            continue
        if c == "\\":
            escape = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    if end is None:
        return None

    array_text = html[start:end]
    try:
        # Escaping vollständig über json auflösen (robuster als manuelles
        # .replace('\\"','"') — behandelt auch \\ , \n , \uXXXX etc. korrekt).
        unescaped = json.loads('"' + array_text + '"')
        return json.loads(unescaped)
    except json.JSONDecodeError:
        return None


def find_first_value(html: str, key: str) -> Optional[str]:
    """Sucht einen einzelnen skalaren Wert wie \\"eventId\\":123, sowohl in
    escapter als auch (zur Sicherheit) in unescapter Form. ACHTUNG: nimmt
    die ERSTE Fundstelle auf der ganzen Seite — auf der Matchseite gibt es
    dafür find_event_id_for_match() darunter, die das NICHT tut (siehe deren
    Docstring, warum das wichtig ist)."""
    m = re.search(rf'\\"{key}\\":(\d+)', html) or re.search(rf'"{key}":(\d+)', html)
    return m.group(1) if m else None


def decode_all_next_f_chunks(html: str) -> list:
    """
    Findet ALLE self.__next_f.push([...])-Aufrufe im HTML, dekodiert das
    JS-String-Escaping sauber über json.JSONDecoder().raw_decode() (siehe
    extract_json_array) und gibt eine flache Liste aller darin enthaltenen,
    bereits als Python-Objekte geparsten JSON-Werte zurück — eine pro
    '<id>:<payload>'-Zeile, die gültiges JSON enthält.
    """
    values = []
    decoder = json.JSONDecoder()
    idx = 0
    while True:
        idx = html.find("self.__next_f.push(", idx)
        if idx == -1:
            break
        call_start = idx + len("self.__next_f.push(")
        try:
            array, _end = decoder.raw_decode(html, call_start)
        except json.JSONDecodeError:
            idx = call_start
            continue
        idx = call_start + 1
        if len(array) >= 2 and isinstance(array[1], str):
            for line in array[1].splitlines():
                m = re.match(r"^[0-9a-fA-F]+:", line)
                if not m:
                    continue
                try:
                    value, _ = decoder.raw_decode(line[m.end():])
                    values.append(value)
                except json.JSONDecodeError:
                    continue
    return values


def _find_dicts_with_key(obj, key: str, found: list) -> None:
    if isinstance(obj, dict):
        if key in obj:
            found.append(obj)
        for v in obj.values():
            _find_dicts_with_key(v, key, found)
    elif isinstance(obj, list):
        for item in obj:
            _find_dicts_with_key(item, key, found)


def find_event_id_for_match(html: str, match_id: int) -> Optional[int]:
    """
    Robuster als eine reine 'erste eventId auf der Seite'-Textsuche: dekodiert
    alle __next_f-Blöcke der Matchseite richtig und sucht gezielt nach einem
    Objekt, das eine 'eventId' enthält UND dessen Inhalt auch die matchId
    dieses konkreten Matches erwähnt. Grund: rft.gg zeigt auf JEDER Seite
    z.B. ein "Aktuelle Events"-Widget im Header mit einer eigenen eventId —
    eine reine Textsuche greift zuverlässig IMMER diese erste, unabhängige
    eventId ab (das war der Bug: alle 311 Matches lieferten dieselbe
    eventId=28). Von mehreren Treffern wird der kompakteste (kleinste)
    Objekt-Block gewählt, da das am ehesten der eigentliche Match-Datensatz
    ist statt ein großer, die ganze Seite umfassender Wrapper.
    """
    candidates = []
    for value in decode_all_next_f_chunks(html):
        found: list = []
        _find_dicts_with_key(value, "eventId", found)
        for d in found:
            try:
                blob = json.dumps(d)
            except (TypeError, ValueError):
                continue
            if str(match_id) in blob:
                candidates.append((len(blob), d["eventId"]))
    candidates.sort()
    return candidates[0][1] if candidates else None


# ---------------------------------------------------------------------------
# Schritt 1: Performances je Spieler+Jahr
# ---------------------------------------------------------------------------
def polite_get(
    session: requests.Session, url: str, max_retries: int = 6, base_delay: float = 2.0
) -> Optional[requests.Response]:
    """
    GET mit Rate-Limit-Handling: bei HTTP 429 wird ein Retry-After-Header
    respektiert, sonst exponentiell länger gewartet (base_delay, *2, *4, ...).
    Gibt bei endgültigem Scheitern None zurück — der Aufrufer darf das NICHT
    wie ein "erfolgreich geladen, aber leer" behandeln (sonst landen
    Fehlschläge fälschlich dauerhaft im Cache, siehe fetch_event_id_for_match).
    """
    delay = base_delay
    for attempt in range(1, max_retries + 1):
        try:
            r = session.get(url, timeout=20)
        except requests.RequestException as e:
            print(f"  {url} -> Netzwerkfehler ({e}), Versuch {attempt}/{max_retries}")
            time.sleep(delay)
            delay *= 2
            continue

        if r.status_code == 429:
            retry_after = r.headers.get("Retry-After")
            wait = float(retry_after) if retry_after else delay
            print(f"  {url} -> HTTP 429, warte {wait:.1f}s (Versuch {attempt}/{max_retries})")
            time.sleep(wait)
            delay *= 2
            continue

        if r.status_code != 200:
            print(f"  {url} -> HTTP {r.status_code}")
            return None

        return r

    print(f"  {url} -> dauerhaft fehlgeschlagen nach {max_retries} Versuchen (übersprungen, NICHT gecached)")
    return None


def fetch_player_performances(
    player_id: int, slug: str, year: int, session: requests.Session, debug: bool = False
) -> list:
    url = f"{BASE_URL}/player/{player_id}-{slug}?year={year}"
    r = polite_get(session, url)
    if r is None:
        return []

    performances = extract_json_array(r.text, "performances")
    if performances is None:
        if debug:
            os.makedirs(DEBUG_DIR, exist_ok=True)
            fname = os.path.join(DEBUG_DIR, f"{slug}_{year}.html")
            with open(fname, "w", encoding="utf-8") as f:
                f.write(r.text)
            print(f"  Kein 'performances'-Array gefunden -> {fname} gespeichert")
        return []
    return performances


# ---------------------------------------------------------------------------
# Schritt 2: eventId je Match (gecacht, damit nichts doppelt geholt wird)
# ---------------------------------------------------------------------------
def load_cache() -> dict[int, Optional[int]]:
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return {int(k): v for k, v in json.load(f).items()}
    return {}


def save_cache(cache: dict[int, Optional[int]]) -> None:
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def fetch_event_id_for_match(
    match_id: int, slug: str, session: requests.Session
) -> tuple[Optional[int], bool]:
    """Rückgabe: (eventId oder None, success). success=False bedeutet
    'Request ist endgültig fehlgeschlagen' — davon darf NIE etwas in den
    Cache geschrieben werden, sonst wird ein Rate-Limit-Fehlschlag
    fälschlich als 'kein Event gefunden' festgeschrieben."""
    url = f"{BASE_URL}/match/{match_id}-{slug}"
    r = polite_get(session, url)
    if r is None:
        return None, False
    return find_event_id_for_match(r.text, match_id), True


def resolve_events(games: list, cache: dict, session: requests.Session) -> None:
    """Ergänzt jedes Game-Dict in-place um 'eventId' und 'category'."""
    unique_matches: "OrderedDict[int, str]" = OrderedDict()
    for g in games:
        unique_matches.setdefault(g["matchId"], g["matchSlug"])

    to_fetch = [(mid, slug) for mid, slug in unique_matches.items() if mid not in cache]
    print(f"  {len(unique_matches)} eindeutige Matches, {len(to_fetch)} davon noch nicht im Cache")

    for i, (match_id, slug) in enumerate(to_fetch, 1):
        print(f"    ({i}/{len(to_fetch)}) Match {match_id} …")
        value, success = fetch_event_id_for_match(match_id, slug, session)
        if success:
            cache[match_id] = value
        time.sleep(REQUEST_DELAY)
        if i % 15 == 0:
            save_cache(cache)  # Zwischenspeichern, falls der Lauf abbricht/erneut blockiert wird
    if to_fetch:
        save_cache(cache)

    for g in games:
        event_id = cache.get(g["matchId"])
        g["eventId"] = event_id
        g["category"] = EVENT_ID_TO_CATEGORY.get(event_id) if event_id is not None else None


# ---------------------------------------------------------------------------
# Orchestrierung
# ---------------------------------------------------------------------------
def collect_all_games(debug: bool = False) -> tuple[list, list]:
    session = requests.Session()
    session.headers.update(HEADERS)
    cache = load_cache()

    all_games: list = []
    for name, player_id in PLAYER_IDS.items():
        if player_id is None:
            print(f"[{name}] übersprungen — noch keine rft.gg-ID in PLAYER_IDS eingetragen")
            continue

        slug = PLAYER_SLUGS.get(name, name.lower())
        for year in YEARS:
            performances = fetch_player_performances(player_id, slug, year, session, debug=debug)
            print(f"[{name}] {year}: {len(performances)} Games")
            for g in performances:
                g["player"] = name
                g["year"] = year
            all_games.extend(performances)
            time.sleep(REQUEST_DELAY)

    print(f"\nInsgesamt {len(all_games)} Games über alle Spieler/Jahre geholt.")

    if all_games:
        print("Löse Turnier-Zuordnung auf (eventId je Match) …")
        resolve_events(all_games, cache, session)
        save_cache(cache)

    relevant = [g for g in all_games if g.get("category")]
    print(f"{len(relevant)} von {len(all_games)} Games gehören zu den konfigurierten Turnieren.")
    return relevant, all_games


def write_csv(games: list, path: str = "rft_games.csv") -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=GAME_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for g in games:
            writer.writerow(g)
    print(f"Geschrieben: {path} ({len(games)} Zeilen)")


if __name__ == "__main__":
    relevant_games, _all_games = collect_all_games(debug=True)
    write_csv(relevant_games)