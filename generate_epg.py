

import os
import re
import sys
import time
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from collections import defaultdict

PLAYLIST_FILE = Path("list1.m3u8")
OUTPUT_FILE = Path("epg1.xml")
MAX_OUTPUT_SIZE = 90 * 1024 * 1024
TIMEOUT = 120

# STAGE 1: These sources always have priority.
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

# STAGE 2: Only used for channels without usable primary data.
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

USER_AGENT = "Mozilla/5.0 (compatible; XMLTV-EPG-Generator/1.0)"


def local_tag(element):
    return element.tag.rsplit("}", 1)[-1]


def normalise(value):
    value = (value or "").lower()
    value = re.sub(r"\([^)]*\)", " ", value)
    value = re.sub(r"\[[^]]*\]", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def read_playlist():
    if not PLAYLIST_FILE.exists():
        raise FileNotFoundError("list1.m3u8 was not found.")

    channels = {}
    current = None

    with PLAYLIST_FILE.open(
        "r", encoding="utf-8-sig", errors="replace"
    ) as file:
        for line in file:
            line = line.strip()

            if line.startswith("#EXTINF:"):
                id_match = re.search(r'tvg-id="([^"]*)"', line, re.I)
                name_match = re.search(r'tvg-name="([^"]*)"', line, re.I)

                tvg_id = id_match.group(1).strip() if id_match else ""
                tvg_name = name_match.group(1).strip() if name_match else ""
                display_name = line.rsplit(",", 1)[-1].strip()

                current = {
                    "names": [
                        name for name in (tvg_name, display_name) if name
                    ]
                }

                if tvg_id:
                    channels.setdefault(tvg_id, current)

            elif line and not line.startswith("#"):
                current = None

    if not channels:
        raise RuntimeError("No channels with tvg-id found in playlist.")

    print(f"Playlist channels with tvg-id: {len(channels)}")
    return channels


def build_name_index(channels):
    index = defaultdict(set)

    for playlist_id, channel in channels.items():
        for name in channel["names"]:
            key = normalise(name)
            if key:
                index[key].add(playlist_id)

    return index


def download_source(url, destination):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT},
    )

    last_error = None

    for attempt in range(1, 4):
        try:
            print(f"\nDownloading: {url} (attempt {attempt}/3)")

            with urllib.request.urlopen(
                request, timeout=TIMEOUT
            ) as response:
                with destination.open("wb") as output:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)

            if destination.stat().st_size == 0:
                raise RuntimeError("Downloaded file is empty.")

            print(f"Downloaded: {destination.stat().st_size:,} bytes")
            return

        except Exception as error:
            last_error = error
            print(f"Download error: {error}")

            if attempt < 3:
                time.sleep(attempt * 3)

    raise RuntimeError(f"Could not download source: {last_error}")


def read_source_mapping(xml_file, name_index, playlist_ids, claimed):
    """Map source XMLTV IDs to unclaimed playlist IDs."""
    mapping = {}

    context = ET.iterparse(xml_file, events=("start", "end"))
    _, root = next(context)

    for event, element in context:
        if event != "end" or local_tag(element) != "channel":
            continue

        source_id = element.get("id", "").strip()

        if source_id:
            if source_id in playlist_ids:
                target = source_id
                if target not in claimed:
                    mapping[source_id] = target

            else:
                names = []

                for child in element:
                    if (
                        local_tag(child) == "display-name"
                        and child.text
                    ):
                        names.append(child.text.strip())

                matches = set()

                for name in names:
                    key = normalise(name)
                    if key:
                        matches.update(name_index.get(key, set()))

                matches -= claimed

                if len(matches) == 1:
                    mapping[source_id] = next(iter(matches))

        element.clear()
        root.clear()

    return mapping


def extract_source_programmes(xml_file, mapping):
    """
    Collect titled programmes by playlist ID.
    A source only claims a channel if it supplies actual programme titles.
    """
    staged = defaultdict(list)
    local_seen = set()
    empty_titles = 0

    context = ET.iterparse(xml_file, events=("start", "end"))
    _, root = next(context)

    for event, element in context:
        if event != "end" or local_tag(element) != "programme":
            continue

        source_id = element.get("channel", "").strip()
        playlist_id = mapping.get(source_id)

        if playlist_id:
            title = ""

            for child in element:
                if local_tag(child) == "title":
                    title = (child.text or "").strip()
                    if title:
                        break

            if not title:
                empty_titles += 1
            else:
                start = element.get("start", "")
                stop = element.get("stop", "")

                key = (playlist_id, start, stop, title)

                if key not in local_seen:
                    copied = ET.fromstring(
                        ET.tostring(element, encoding="utf-8")
                    )
                    copied.set("channel", playlist_id)

                    staged[playlist_id].append(copied)
                    local_seen.add(key)

        element.clear()
        root.clear()

    return staged, empty_titles


def process_source(url, source_number, temp_dir, channels,
                   name_index, claimed, output_root, global_seen):
    """Process one source without allowing it to overwrite prior sources."""
    xml_file = Path(temp_dir) / f"source_{source_number}.xml"

    try:
        download_source(url, xml_file)

        mapping = read_source_mapping(
            xml_file,
            name_index,
            set(channels),
            claimed,
        )

        if not mapping:
            print("No unclaimed playlist channels matched.")
            return 0

        print(f"Matched source channel IDs: {len(mapping)}")

        staged, empty_titles = extract_source_programmes(
            xml_file, mapping
        )

        added = 0

        for playlist_id, programmes in staged.items():
            # Only claim the channel when this source has titled data.
            if not programmes:
                continue

            if playlist_id in claimed:
                continue

            channel_added = 0

            for programme in programmes:
                key = (
                    playlist_id,
                    programme.get("start", ""),
                    programme.get("stop", ""),
                    (programme.findtext("title") or "").strip(),
                )

                if key not in global_seen:
                    output_root.append(programme)
                    global_seen.add(key)
                    channel_added += 1

            if channel_added:
                claimed.add(playlist_id)
                added += channel_added
                print(
                    f"Claimed {playlist_id}: "
                    f"{channel_added} titled programmes"
                )

        print(f"Skipped empty titles: {empty_titles}")
        print(f"Added from source: {added}")
        return added

    except Exception as error:
        print(f"SOURCE ERROR: {url}")
        print(error)
        return 0


def main():
    channels = read_playlist()
    name_index = build_name_index(channels)

    output_root = ET.Element("tv", {
        "source-info-name": "Automatic Two-Stage EPG",
        "generator-info-name": "GitHub Actions",
    })

    claimed = set()
    global_seen = set()
    total_added = 0

    with tempfile.TemporaryDirectory() as temp_dir:

        print("\n========== STAGE 1: PRIMARY SOURCES ==========")

        for number, url in enumerate(PRIMARY_SOURCES, start=1):
            added = process_source(
                url,
                number,
                temp_dir,
                channels,
                name_index,
                claimed,
                output_root,
                global_seen,
            )
            total_added += added

        primary_channels = set(claimed)

        print("\n========== STAGE 2: FALLBACK SOURCES ==========")
        print(
            f"Channels already covered by primary sources: "
            f"{len(primary_channels)}"
        )

        for number, url in enumerate(FALLBACK_SOURCES, start=101):
            if len(claimed) == len(channels):
                print("All playlist channels already have guide data.")
                break

            added = process_source(
                url,
                number,
                temp_dir,
                channels,
                name_index,
                claimed,
                output_root,
                global_seen,
            )
            total_added += added

    if not output_root.findall("programme"):
        raise RuntimeError(
            "No titled programmes found. Previous epg1.xml was preserved."
        )

    # Sort programmes chronologically.
    programmes = output_root.findall("programme")
    programmes.sort(
        key=lambda p: (
            p.get("start", ""),
            p.get("channel", ""),
        )
    )

    # Create only the channel entries that have programme data.
    final_root = ET.Element("tv", output_root.attrib)

    for playlist_id in sorted(claimed):
        channel_element = ET.SubElement(
            final_root, "channel", {"id": playlist_id}
        )

        names = channels[playlist_id]["names"]
        display_name = names[0] if names else playlist_id

        ET.SubElement(
            channel_element, "display-name"
        ).text = display_name

    for programme in programmes:
        final_root.append(programme)

    temporary_output = OUTPUT_FILE.with_name("epg1.tmp.xml")

    ET.ElementTree(final_root).write(
        temporary_output,
        encoding="utf-8",
        xml_declaration=True,
    )

    # Validate the generated file before replacing the existing one.
    check_root = ET.parse(temporary_output).getroot()
    check_channels = check_root.findall("channel")
    check_programmes = check_root.findall("programme")

    channel_ids = {c.get("id") for c in check_channels}

    titled = [
        p for p in check_programmes
        if (p.findtext("title") or "").strip()
    ]

    unmatched = {
        p.get("channel") for p in check_programmes
    } - channel_ids

    if not check_channels or not titled or unmatched:
        temporary_output.unlink(missing_ok=True)
        raise RuntimeError("Generated EPG failed validation.")

    size = temporary_output.stat().st_size

    if size > MAX_OUTPUT_SIZE:
        temporary_output.unlink(missing_ok=True)
        raise RuntimeError(
            f"Generated EPG is too large: {size:,} bytes. "
            "Previous epg1.xml was preserved."
        )

    os.replace(temporary_output, OUTPUT_FILE)

    print("\n========== GENERATION COMPLETE ==========")
    print(f"Playlist channels: {len(channels)}")
    print(f"Primary source channels: {len(primary_channels)}")
    print(f"Fallback source channels: {len(claimed - primary_channels)}")
    print(f"Total channels with guide data: {len(claimed)}")
    print(f"Programmes with titles: {len(titled)}")
    print(f"Output size: {size:,} bytes")
    print(f"Saved as: {OUTPUT_FILE}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"\nFATAL ERROR: {error}", file=sys.stderr)
        raise
