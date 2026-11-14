#!/usr/bin/env python3
"""
Fußball-Kalender: führt die Spielpläne mehrerer Mannschaften von FuPa
zu EINEM Kalender-Abo (docs/fussball.ics) zusammen.

- Die passende Saison wird automatisch gesucht (z. B. 2026-27, 2027-28 ...),
  damit das Abo nach dem Saisonwechsel ohne Zutun weiterläuft.
- Fällt eine Quelle aus, bleiben die zuletzt bekannten Spiele dieses Teams erhalten.
- Spiele ohne feste Anstoßzeit werden als ganztägige Termine eingetragen.

Nur Python-Standardbibliothek, keine Installation nötig.
"""

import datetime as dt
import pathlib
import re
import sys
import urllib.request
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Mannschaften: (Kürzel im Kalender, FuPa-Vereinskennung, FuPa-Teamkennung)
# Teamkennung: m1 = 1. Herren, m2 = 2. Herren
# ---------------------------------------------------------------------------
TEAMS = [
    ("Bayern", "fc-bayern-muenchen", "m1"),
    ("Aachen", "alemannia-aachen", "m1"),
    ("Golzheim 1", "fc-golzheim", "m1"),
    ("Golzheim 2", "fc-golzheim", "m2"),
]

CAL_NAME = "Fußball: Bayern, Aachen, Golzheim"
OUTPUT = pathlib.Path(__file__).parent / "docs" / "fussball.ics"
STAND = pathlib.Path(__file__).parent / "docs" / "stand.txt"
FEED_URL = "https://api.fupa.net/v1/teams/{club}-{team}-{season}/matches.ics"
BERLIN = ZoneInfo("Europe/Berlin")
UTC = dt.timezone.utc
FIXED_DTSTAMP = "DTSTAMP:20260101T000000Z"


# ---------------------------------------------------------------- Hilfen ---
def unfold(text):
    """iCalendar-Zeilen zusammenfügen (RFC 5545: Fortsetzungszeilen beginnen mit Leerzeichen/Tab)."""
    lines = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw:
            lines.append(raw)
    return lines


def fold(line):
    """Lange Zeilen wieder falten (max. 75 Bytes)."""
    out, cur = [], ""
    for ch in line:
        limit = 75 if not out else 74
        if len((cur + ch).encode("utf-8")) > limit:
            out.append(cur)
            cur = ch
        else:
            cur += ch
    out.append(cur)
    return "\r\n ".join(out)


def blocks(lines, name):
    """Alle BEGIN:name ... END:name Blöcke (als Zeilenlisten) liefern."""
    res, cur, depth = [], None, 0
    for ln in lines:
        if ln == f"BEGIN:{name}":
            if cur is None:
                cur, depth = [], 0
            depth += 1
        if cur is not None:
            cur.append(ln)
        if ln == f"END:{name}" and cur is not None:
            depth -= 1
            if depth == 0:
                res.append(cur)
                cur = None
    return res


def prop(event, key):
    """(Parameter, Wert) einer Eigenschaft holen, z. B. DTSTART;TZID=Europe/Berlin:2026..."""
    for ln in event:
        name, _, value = ln.partition(":")
        base, *params = name.split(";")
        if base.upper() == key:
            return params, value
    return None, None


def parse_start(event):
    """DTSTART als (datetime mit Zeitzone | date, ist_ganztägig)."""
    params, value = prop(event, "DTSTART")
    if value is None:
        return None, False
    if any(p.upper() == "VALUE=DATE" for p in params) or len(value) == 8:
        return dt.datetime.strptime(value[:8], "%Y%m%d").date(), True
    tz = BERLIN
    for p in params:
        if p.upper().startswith("TZID="):
            try:
                tz = ZoneInfo(p.split("=", 1)[1].strip('"'))
            except Exception:
                tz = BERLIN
    if value.endswith("Z"):
        return dt.datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC), False
    return dt.datetime.strptime(value[:15], "%Y%m%dT%H%M%S").replace(tzinfo=tz), False


def is_future(event, now):
    start, allday = parse_start(event)
    if start is None:
        return False
    if allday:
        return start >= now.date()
    return start >= now


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "fussball-kalender/1.0 (privates Kalender-Abo)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def season_candidates(today):
    """Neueste Saison zuerst, z. B. ['2027-28', '2026-27', '2025-26']."""
    y = today.year
    return [f"{s}-{(s + 1) % 100:02d}" for s in (y + 1, y, y - 1)]


# --------------------------------------------------- Team-Feed bestimmen ---
def load_team(club, team, now, fetcher=fetch):
    """Lädt alle Saison-Kandidaten. Übernommen werden alle Saisons mit kommenden Spielen
    (wichtig rund um den Saisonwechsel); gibt es keine, die neueste Saison mit Spielen
    (Sommerpause). Liefert (saisons, events, vtimezones) oder None."""
    found = []  # (saison, events, vtimezones, kommende)
    for season in season_candidates(now.date()):
        url = FEED_URL.format(club=club, team=team, season=season)
        try:
            lines = unfold(fetcher(url))
        except Exception as e:
            print(f"  {season}: nicht verfügbar ({e.__class__.__name__})")
            continue
        events = blocks(lines, "VEVENT")
        if not events:
            print(f"  {season}: keine Spiele")
            continue
        future = sum(is_future(e, now) for e in events)
        print(f"  {season}: {len(events)} Spiele, davon {future} kommend")
        found.append((season, events, blocks(lines, "VTIMEZONE"), future))
    if not found:
        return None
    chosen = [f for f in found if f[3] > 0] or [found[0]]  # found ist neueste zuerst
    seen, events, tzs = set(), [], []
    for season, evs, tz, _ in chosen:
        tzs.extend(tz)
        for e in evs:
            uid = prop(e, "UID")[1] or id(e)
            if uid not in seen:
                seen.add(uid)
                events.append(e)
    return ", ".join(f[0] for f in chosen), events, tzs


# ------------------------------------------------- Termin aufbereiten -----
def transform(event, label, key):
    """Kürzel vor den Titel setzen, UID eindeutig machen, offene Anstoßzeiten ganztägig machen."""
    start, allday = parse_start(event)
    tbd = (not allday and start is not None
           and start.astimezone(UTC).time() == dt.time(0, 0))

    out = []
    for ln in event:
        name, _, value = ln.partition(":")
        base = name.split(";")[0].upper()
        if base == "SUMMARY":
            title = value
            if tbd:
                title += " (Anstoßzeit offen)"
            out.append(f"SUMMARY:{label} | {title}")
        elif base == "UID":
            out.append(f"UID:{key}-{value}")
        elif base == "DTSTAMP":
            out.append(FIXED_DTSTAMP)
        elif base in ("DTSTART", "DTEND", "DURATION") and tbd:
            if base == "DTSTART":
                day = start.astimezone(BERLIN).date()
                out.append(f"DTSTART;VALUE=DATE:{day:%Y%m%d}")
                out.append(f"DTEND;VALUE=DATE:{day + dt.timedelta(days=1):%Y%m%d}")
            # DTEND/DURATION der Originalzeit entfallen
        elif base == "X-FK-TEAM":
            continue
        else:
            out.append(ln)
    if not any(l.startswith("UID:") for l in out):
        s = prop(event, "DTSTART")[1] or "x"
        out.insert(1, f"UID:{key}-{s}@fussball-kalender")
    if not any(l.startswith("DTSTAMP") for l in out):
        out.insert(1, FIXED_DTSTAMP)
    out.insert(-1, f"X-FK-TEAM:{key}")
    return out


def previous_events():
    """Bisher veröffentlichte Termine je Team (Fallback bei Ausfall einer Quelle)."""
    if not OUTPUT.exists():
        return {}, []
    lines = unfold(OUTPUT.read_text(encoding="utf-8"))
    by_team = {}
    for ev in blocks(lines, "VEVENT"):
        _, key = prop(ev, "X-FK-TEAM")
        if key:
            by_team.setdefault(key, []).append(ev)
    return by_team, blocks(lines, "VTIMEZONE")


def team_key(club, team):
    return f"{club}-{team}"


# ----------------------------------------------------------- Hauptteil ---
def build(now=None, fetcher=fetch):
    now = now or dt.datetime.now(UTC)
    old_events, old_tz = previous_events()
    all_events, timezones, failed = [], {}, []

    for label, club, team in TEAMS:
        key = team_key(club, team)
        print(f"{label} ({key}):")
        result = load_team(club, team, now, fetcher)
        if result is None:
            kept = old_events.get(key, [])
            print(f"  -> Quelle nicht erreichbar, behalte {len(kept)} bekannte Spiele")
            failed.append(label)
            all_events.extend(kept)
            for tz in old_tz:
                timezones.setdefault(prop(tz, "TZID")[1], tz)
            continue
        season, events, tzs = result
        print(f"  -> verwende Saison {season}")
        for tz in tzs:
            timezones.setdefault(prop(tz, "TZID")[1], tz)
        all_events.extend(transform(e, label, key) for e in events)

    def sort_key(ev):
        s, allday = parse_start(ev)
        if s is None:
            return dt.datetime.max.replace(tzinfo=UTC)
        return dt.datetime.combine(s, dt.time(12), BERLIN) if allday else s

    all_events.sort(key=sort_key)

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//fussball-kalender//DE",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{CAL_NAME}",
        "X-WR-TIMEZONE:Europe/Berlin",
        "REFRESH-INTERVAL;VALUE=DURATION:PT3H",
        "X-PUBLISHED-TTL:PT3H",
    ]
    for tz in timezones.values():
        lines.extend(tz)
    for ev in all_events:
        lines.extend(ev)
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(l) for l in lines) + "\r\n", len(all_events), failed


def main():
    ics, count, failed = build()
    if count == 0:
        print("Keine Spiele gefunden – bestehende Datei bleibt unverändert.")
        return 1
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    changed = not OUTPUT.exists() or OUTPUT.read_text(encoding="utf-8") != ics
    if changed:
        OUTPUT.write_text(ics, encoding="utf-8", newline="")
    # Einmal pro Tag ein kleiner Eintrag, damit GitHub die Automatik nicht wegen Inaktivität pausiert.
    STAND.write_text(f"Zuletzt geprüft: {dt.date.today():%d.%m.%Y}\n", encoding="utf-8")
    print(f"{count} Spiele im Kalender, {'aktualisiert' if changed else 'unverändert'}.")
    if failed:
        print("Nicht erreichbar: " + ", ".join(failed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
