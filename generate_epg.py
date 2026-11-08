#!/usr/bin/env python3

import os
import re
import sys
import json
import time
import tempfile
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET

from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from collections import defaultdict

# --------------------------------------------------
# SETTINGS
# --------------------------------------------------

PLAYLIST_FILE = Path("list1.m3u8")
OUTPUT_FILE = Path("epg.xml")
CACHE_FILE = Path("football_fixtures_cache.json")

MAX_OUTPUT_SIZE = 90 * 1024 * 1024
TIMEOUT = 120

WARSAW = ZoneInfo("Europe/Warsaw")

API_KEY = os.environ.get("API_FOOTBALL_KEY", "").strip()
API_BASE = "https://v3.football.api-sports.io"

# --------------------------------------------------
# INTERNATIONAL EPG SOURCES
# --------------------------------------------------

PRIMARY_SOURCES = [
    "https://iptv-epg.org/files/epg-gb.xml",
    "https://iptv-epg.org/files/epg-ar.xml",
    "https://iptv-epg.org/files/epg-fr.xml",
    "https://iptv-epg.org/files/epg-cz.xml",
    "https://iptv-epg.org/files/epg-be.xml",
    "https://iptv-epg.org/files/epg-de.xml",
    "https://iptv-epg.org/files/epg-us.xml",
    "https://iptv-epg.org/files/epg-es.xml",
    "https://iptv-epg.org/files/epg-ch.xml",
]

FALLBACK_SOURCES = [
    "https://www.open-epg.com/files/belgium.xml",
    "https://www.open-epg.com/files/czech3.xml",
    "https://www.open-epg.com/files/france.xml",
    "https://www.open-epg.com/files/italy1.xml",
    "https://www.open-epg.com/files/saudiarabia.xml",
    "https://www.open-epg.com/files/spain.xml",
    "https://www.open-epg.com/files/unitedkingdom.xml",
    "https://www.open-epg.com/files/switzerland4.xml",
    "https://www.open-epg.com/files/slovakia1.xml",
]

# --------------------------------------------------
# FOOTBALL CLUBS
# --------------------------------------------------

# tvg-id : (club name, API-Football team ID)

FOOTBALL_TEAMS = {
    "Arsenal.FC": ("Arsenal", 42),
    "Chelsea.FC": ("Chelsea", 49),
    "Liverpool.FC": ("Liverpool", 40),
    "ManCity.FC": ("Manchester City", 50),
    "ManUnited.FC": ("Manchester United", 33),
    "Tottenham.FC": ("Tottenham Hotspur", 47),

    "AtleticoMadrid.FC": ("Atletico Madrid", 530),
    "Barcelona.FC": ("Barcelona", 529),
    "RealMadrid.FC": ("Real Madrid", 541),
}

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/xml,text/xml,*/*",
}


def log(message):
    print(message, flush=True)


# --------------------------------------------------
# GENERAL HELPERS
# --------------------------------------------------

def download(url):
    request = urllib.request.Request(url, headers=HEADERS)

    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read()


def normalize(value):
    value = (value or "").lower()
    value = re.sub(r"\([^)]*\)|\[[^]]*\]", " ", value)
    return re.sub(r"[^a-z0-9]", "", value)


def parse_playlist():
    if not PLAYLIST_FILE.exists():
        raise FileNotFoundError("list1.m3u8 was not found")

    channels = {}
    current = None

    with PLAYLIST_FILE.open(
        "r", encoding="utf-8-sig", errors="replace"
    ) as file:

        for raw in file:
            line = raw.strip()

            if line.startswith("#EXTINF:"):

                id_match = re.search(
                    r'\btvg-id="([^"]*)"', line
                )

                logo_match = re.search(
                    r'\btvg-logo="([^"]*)"', line
                )

                group_match = re.search(
                    r'\bgroup-title="([^"]*)"', line
                )

                name = (
                    line.split(",", 1)[1].strip()
                    if "," in line else ""
                )

                current = {
                    "id": id_match.group(1).strip()
                    if id_match else "",

                    "name": name,

                    "logo": logo_match.group(1).strip()
                    if logo_match else "",

                    "group": group_match.group(1).strip()
                    if group_match else "",
                }

            elif line and not line.startswith("#"):

                if current and current["id"]:
                    channels.setdefault(
                        current["id"], current
                    )

                current = None

    if not channels:
        raise RuntimeError(
            "No channels with tvg-id were found in list1.m3u8"
        )

    return channels


def xmltv_stamp(dt):
    return dt.astimezone(WARSAW).strftime(
        "%Y%m%d%H%M%S %z"
    )


# --------------------------------------------------
# INTERNATIONAL XMLTV
# --------------------------------------------------

def read_source(url):
    log(f"Downloading: {url}")

    data = download(url)
    root = ET.fromstring(data)

    source_channels = {}
    programmes = []

    for channel in root.findall("channel"):
        channel_id = channel.get("id")

        if channel_id:
            names = [
                item.text or ""
                for item in channel.findall("display-name")
            ]

            source_channels[channel_id] = names

    for programme in root.findall("programme"):

        channel_id = programme.get("channel")
        start = programme.get("start", "")
        stop = programme.get("stop", "")
        title = (programme.findtext("title") or "").strip()

        if channel_id and start and stop and title:
            programmes.append(
                (channel_id, programme)
            )

    return source_channels, programmes


def match_source_channels(
    source_channels,
    playlist_channels,
    claimed
):
    matches = {}

    for source_id, display_names in source_channels.items():

        # Exact tvg-id match first.
        if (
            source_id in playlist_channels
            and source_id not in claimed
        ):
            matches[source_id] = source_id
            continue

        source_names = {
            normalize(name)
            for name in display_names
            if name
        }

        for playlist_id, channel in playlist_channels.items():

            if playlist_id in claimed:
                continue

            playlist_name = normalize(channel["name"])

            if playlist_name and playlist_name in source_names:
                matches[source_id] = playlist_id
                break

    return matches


def add_xmltv_source(
    root,
    playlist_channels,
    claimed,
    seen,
    url
):
    try:
        source_channels, programmes = read_source(url)

        matches = match_source_channels(
            source_channels,
            playlist_channels,
            claimed
        )

        additions = []
        counts = defaultdict(int)

        for source_id, programme in programmes:

            target_id = matches.get(source_id)

            if not target_id:
                continue

            title = (programme.findtext("title") or "").strip()

            key = (
                target_id,
                programme.get("start"),
                programme.get("stop"),
                title,
            )

            if key in seen:
                continue

            seen.add(key)

            copied = ET.fromstring(
                ET.tostring(programme, encoding="utf-8")
            )

            copied.set("channel", target_id)

            additions.append(copied)
            counts[target_id] += 1

        useful_channels = {
            channel_id
            for channel_id, count in counts.items()
            if count
        }

        for channel_id in useful_channels:
            claimed.add(channel_id)

        for programme in additions:
            if programme.get("channel") in useful_channels:
                root.append(programme)

        log(
            f"Matched {len(useful_channels)} channels; "
            f"added {len(additions)} programmes"
        )

    except Exception as error:
        log(f"Source skipped: {error}")


# --------------------------------------------------
# API-FOOTBALL
# --------------------------------------------------

def api_request(params):
    if not API_KEY:
        raise RuntimeError(
            "API_FOOTBALL_KEY GitHub secret is missing"
        )

    query = urllib.parse.urlencode(params)

    url = f"{API_BASE}/fixtures?{query}"

    request = urllib.request.Request(
        url,
        headers={
            "x-apisports-key": API_KEY,
            "User-Agent": "XMLTV-EPG-Generator",
        }
    )

    with urllib.request.urlopen(
        request, timeout=TIMEOUT
    ) as response:

        result = json.loads(
            response.read().decode("utf-8")
        )

    if result.get("errors"):
        raise RuntimeError(str(result["errors"]))

    return result


def current_season():
    now = datetime.now(WARSAW)

    return (
        now.year
        if now.month >= 7
        else now.year - 1
    )


def load_cache():
    if CACHE_FILE.exists():
        try:
            return json.loads(
                CACHE_FILE.read_text(encoding="utf-8")
            )
        except Exception:
            log("Cache unreadable; starting a new cache.")

    return {
        "season": None,
        "updated": 0,
        "teams": {},
        "fixtures": {},
    }


def save_cache(cache):
    CACHE_FILE.write_text(
        json.dumps(
            cache,
            ensure_ascii=False,
            separators=(",", ":")
        ),
        encoding="utf-8"
    )


def fetch_team_season(team_id, season):
    fixtures = []
    page = 1

    while True:

        result = api_request({
            "team": team_id,
            "season": season,
            "timezone": "Europe/Warsaw",
            "page": page,
        })

        fixtures.extend(result.get("response", []))

        paging = result.get("paging", {})
        total_pages = int(paging.get("total", 1))

        if page >= total_pages:
            break

        page += 1

    return fixtures


def rebuild_fixture_index(cache):
    indexed = {}

    for rows in cache.get("teams", {}).values():

        for row in rows:

            fixture_id = (
                row.get("fixture", {}).get("id")
            )

            if fixture_id is not None:
                indexed[str(fixture_id)] = row

    cache["fixtures"] = indexed


def refresh_season_cache(cache):
    season = current_season()
    now = int(time.time())

    cache_is_fresh = (
        cache.get("season") == season
        and now - int(cache.get("updated", 0)) < 86400
    )

    if cache_is_fresh:
        log("Using cached season fixtures.")
        return cache

    log(
        f"Refreshing {season}/{season + 1} "
        "season fixtures..."
    )

    all_successful = True

    for channel_id, (club, team_id) in FOOTBALL_TEAMS.items():

        try:
            rows = fetch_team_season(team_id, season)

            cache.setdefault("teams", {})[channel_id] = rows

            log(f"{club}: {len(rows)} fixtures")

        except Exception as error:
            all_successful = False
            log(f"{club} season refresh failed: {error}")

    cache["season"] = season

    if all_successful:
        cache["updated"] = now

    rebuild_fixture_index(cache)

    return cache


def refresh_today_and_live(cache):
    today = datetime.now(WARSAW).date().isoformat()

    # Today's matches and results for each club.
    for channel_id, (club, team_id) in FOOTBALL_TEAMS.items():

        try:
            result = api_request({
                "team": team_id,
                "date": today,
                "timezone": "Europe/Warsaw",
            })

            for row in result.get("response", []):

                fixture_id = row.get(
                    "fixture", {}
                ).get("id")

                if fixture_id is not None:
                    cache.setdefault(
                        "fixtures", {}
                    )[str(fixture_id)] = row

        except Exception as error:
            log(f"Today's fixtures for {club} failed: {error}")

    # One request to refresh live matches and scores.
    try:
        result = api_request({
            "live": "all",
            "timezone": "Europe/Warsaw",
        })

        for row in result.get("response", []):

            fixture_id = row.get(
                "fixture", {}
            ).get("id")

            if fixture_id is not None:
                cache.setdefault(
                    "fixtures", {}
                )[str(fixture_id)] = row

    except Exception as error:
        log(f"Live fixture refresh failed: {error}")

    return cache


# --------------------------------------------------
# FOOTBALL XMLTV PROGRAMMES
# --------------------------------------------------

def create_football_programme(row, channel_id):

    fixture = row.get("fixture") or {}
    teams = row.get("teams") or {}
    league = row.get("league") or {}
    goals = row.get("goals") or {}

    date_text = fixture.get("date")

    if not date_text:
        return None

    try:
        start = datetime.fromisoformat(
            date_text.replace("Z", "+00:00")
        ).astimezone(WARSAW)

    except ValueError:
        return None

    home = (teams.get("home") or {}).get(
        "name", "Home"
    )

    away = (teams.get("away") or {}).get(
        "name", "Away"
    )

    home_goals = goals.get("home")
    away_goals = goals.get("away")

    score = ""

    if home_goals is not None and away_goals is not None:
        score = f"{home_goals}-{away_goals}"

    status = (
        fixture.get("status") or {}
    ).get("short", "NS")

    live_statuses = {
        "1H", "HT", "2H", "ET", "BT", "P", "LIVE"
    }

    finished_statuses = {
        "FT", "AET", "PEN"
    }

    cancelled_statuses = {
        "PST", "CANC", "ABD", "AWD", "WO"
    }

    if status in live_statuses:

        title = (
            f"LIVE {score}: {home} vs {away}"
            if score
            else f"LIVE: {home} vs {away}"
        )

    elif status in finished_statuses:

        title = (
            f"{home} {score} {away}"
            if score
            else f"{home} vs {away} (FT)"
        )

    elif status in cancelled_statuses:

        title = f"{home} vs {away} ({status})"

    else:
        title = f"{home} vs {away}"

    competition = league.get("name") or "Football"
    country = league.get("country") or ""
    round_name = league.get("round") or ""

    venue = (
        fixture.get("venue") or {}
    ).get("name", "")

    status_long = (
        fixture.get("status") or {}
    ).get("long", status)

    description = [
        competition,
        country,
        round_name,
        f"Status: {status_long}",
    ]

    if score:
        description.append(f"Score: {score}")

    if venue:
        description.append(f"Venue: {venue}")

    programme = ET.Element(
        "programme",
        {
            "start": xmltv_stamp(start),
            "stop": xmltv_stamp(
                start + timedelta(minutes=150)
            ),
            "channel": channel_id,
        }
    )

    ET.SubElement(
        programme, "title", {"lang": "en"}
    ).text = title

    ET.SubElement(
        programme, "sub-title", {"lang": "en"}
    ).text = competition

    ET.SubElement(
        programme, "desc", {"lang": "en"}
    ).text = " | ".join(
        part for part in description if part
    )

    ET.SubElement(
        programme, "category", {"lang": "en"}
    ).text = "Sports"

    ET.SubElement(
        programme, "category", {"lang": "en"}
    ).text = "Football"

    return programme


def add_football_programmes(
    root,
    playlist_channels,
    cache,
    seen
):
    if not API_KEY:
        log(
            "API key missing: skipping football fixtures. "
            "International EPG sources will still be processed."
        )
        return

    for channel_id, (club, team_id) in FOOTBALL_TEAMS.items():

        if channel_id not in playlist_channels:
            log(
                f"WARNING: {club} is missing tvg-id "
                f'"{channel_id}" in list1.m3u8'
            )
            continue

        club_fixtures = {}

        # Full-season fixtures from the cache.
        for row in cache.get(
            "teams", {}
        ).get(channel_id, []):

            fixture_id = (
                row.get("fixture", {}).get("id")
            )

            if fixture_id is not None:
                club_fixtures[str(fixture_id)] = row

        # Overlay today's and live updates.
        for fixture_id, row in cache.get(
            "fixtures", {}
        ).items():

            teams = row.get("teams") or {}

            home_id = str(
                (teams.get("home") or {}).get("id")
            )

            away_id = str(
                (teams.get("away") or {}).get("id")
            )

            if str(team_id) in {home_id, away_id}:
                club_fixtures[str(fixture_id)] = row

        added = 0

        for row in club_fixtures.values():

            programme = create_football_programme(
                row, channel_id
            )

            if programme is None:
                continue

            key = (
                channel_id,
                programme.get("start"),
                programme.findtext("title"),
            )

            if key in seen:
                continue

            seen.add(key)
            root.append(programme)
            added += 1

        log(f"{club}: added {added} football programmes")


# --------------------------------------------------
# MAIN
# --------------------------------------------------

def main():

    playlist_channels = parse_playlist()

    log(
        f"Found {len(playlist_channels)} playlist "
        "channels with tvg-id."
    )

    root = ET.Element(
        "tv",
        {"generator-info-name": "Automatic XMLTV EPG"}
    )

    # Create every playlist channel.
    for channel_id, channel_data in playlist_channels.items():

        channel = ET.SubElement(
            root, "channel", {"id": channel_id}
        )

        ET.SubElement(
            channel, "display-name"
        ).text = channel_data["name"] or channel_id

        if channel_data["logo"]:
            ET.SubElement(
                channel,
                "icon",
                {"src": channel_data["logo"]}
            )

    claimed = set()
    seen = set()

    # Primary sources first.
    for url in PRIMARY_SOURCES:

        add_xmltv_source(
            root,
            playlist_channels,
            claimed,
            seen,
            url
        )

    # Fallback sources for channels still unmatched.
    for url in FALLBACK_SOURCES:

        if len(claimed) >= len(playlist_channels):
            break

        add_xmltv_source(
            root,
            playlist_channels,
            claimed,
            seen,
            url
        )

    # Football data.
    cache = load_cache()

    if API_KEY:

        try:
            cache = refresh_season_cache(cache)
            cache = refresh_today_and_live(cache)
            save_cache(cache)

        except Exception as error:
            log(f"Football API refresh problem: {error}")

    add_football_programmes(
        root,
        playlist_channels,
        cache,
        seen
    )

    # Sort all programmes chronologically.
    programmes = root.findall("programme")

    programmes.sort(
        key=lambda item: (
            item.get("start", ""),
            item.get("channel", "")
        )
    )

    for programme in programmes:
        root.remove(programme)

    root.extend(programmes)

    # Write safely using a temporary file.
    fd, temporary = tempfile.mkstemp(
        prefix="epg-",
        suffix=".xml",
        dir="."
    )

    os.close(fd)

    try:

        tree = ET.ElementTree(root)
        ET.indent(tree, space="  ")

        tree.write(
            temporary,
            encoding="utf-8",
            xml_declaration=True
        )

        size = os.path.getsize(temporary)

        if size >= MAX_OUTPUT_SIZE:
            raise RuntimeError(
                f"EPG exceeds 90 MiB: {size:,} bytes"
            )

        # Confirm that the generated XML is valid.
        ET.parse(temporary)

        os.replace(temporary, OUTPUT_FILE)

    finally:

        if os.path.exists(temporary):
            os.remove(temporary)

    log("----------------------------------")
    log("EPG GENERATION SUCCESSFUL")
    log(f"Channels: {len(root.findall('channel'))}")
    log(f"Programmes: {len(root.findall('programme'))}")
    log(f"File size: {os.path.getsize(OUTPUT_FILE):,} bytes")
    log(f"Output: {OUTPUT_FILE}")
    log("----------------------------------")


if __name__ == "__main__":

    try:
        main()

    except Exception as error:
        log(f"ERROR: {error}")
        sys.exit(1)
