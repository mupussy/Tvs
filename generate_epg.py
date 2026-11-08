
import os
import re
import sys
import time
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

PLAYLIST_FILE = Path("list1.m3u8")
OUTPUT_FILE = Path("epg1.xml")

EPG_SOURCES = [
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

USER_AGENT = "Mozilla/5.0 (compatible; XMLTV-EPG-Generator/1.0)"
TIMEOUT = 90


def local_tag(element):
    """Remove an optional XML namespace from a tag."""
    return element.tag.rsplit("}", 1)[-1]


def normalise(value):
    """Normalise channel names for safe exact-name matching."""
    value = (value or "").lower()
    value = re.sub(r"\([^)]*\)", " ", value)
    value = re.sub(r"\[[^]]*\]", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def read_playlist(path):
    if not path.exists():
        raise FileNotFoundError(f"Playlist not found: {path}")

    channels = {}
    current = None

    with path.open("r", encoding="utf-8-sig", errors="replace") as file:
        for line in file:
            line = line.strip()

            if line.startswith("#EXTINF:"):
                id_match = re.search(r'tvg-id="([^"]*)"', line, re.I)
                name_match = re.search(r'tvg-name="([^"]*)"', line, re.I)

                display_name = line.rsplit(",", 1)[-1].strip()

                tvg_id = id_match.group(1).strip() if id_match else ""
                tvg_name = name_match.group(1).strip() if name_match else ""

                current = {
                    "id": tvg_id,
                    "names": [x for x in (tvg_name, display_name) if x],
                }

                if tvg_id:
                    channels.setdefault(tvg_id, current)

            elif line and not line.startswith("#"):
                current = None

    if not channels:
        raise RuntimeError("No channels with tvg-id were found in list1.m3u8")

    print(f"Playlist channels with tvg-id: {len(channels)}")
    return channels


def build_playlist_name_index(channels):
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
            print(f"Downloading {url} (attempt {attempt}/3)")

            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                with destination.open("wb") as output:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)

            if destination.stat().st_size == 0:
                raise RuntimeError("Downloaded file is empty")

            print(f"Downloaded {destination.stat().st_size:,} bytes")
            return

        except Exception as error:
            last_error = error
            print(f"Download failed: {error}")

            if attempt < 3:
                time.sleep(attempt * 3)

    raise RuntimeError(f"Could not download {url}: {last_error}")


def read_source_channel_mapping(xml_file, playlist_name_index, playlist_ids):
    """Read XMLTV channel IDs and map them to playlist IDs."""
    mapping = {}

    for event, element in ET.iterparse(xml_file, events=("end",)):
        if local_tag(element) == "channel":
            source_id = element.get("id", "").strip()

            if source_id:
                # Exact ID match takes priority.
                if source_id in playlist_ids:
                    mapping[source_id] = source_id
                else:
                    names = []

                    for child in element:
                        if local_tag(child) == "display-name":
                            if child.text:
                                names.append(child.text.strip())

                    matches = set()

                    for name in names:
                        key = normalise(name)
                        if key:
                            matches.update(playlist_name_index.get(key, set()))

                    # Only use a name match when it is unambiguous.
                    if len(matches) == 1:
                        mapping[source_id] = next(iter(matches))

            element.clear()

    return mapping


def extract_programmes(xml_file, source_mapping, output_root, seen):
    """Copy complete programmes with non-empty titles."""
    added = 0
    skipped_empty_titles = 0

    for event, element in ET.iterparse(xml_file, events=("end",)):
        if local_tag(element) != "programme":
            continue

        source_id = element.get("channel", "").strip()
        playlist_id = source_mapping.get(source_id)

        if playlist_id:
            title = ""

            for child in element:
                if local_tag(child) == "title":
                    title = (child.text or "").strip()
                    if title:
                        break

            if not title:
                skipped_empty_titles += 1
            else:
                start = element.get("start", "")
                stop = element.get("stop", "")

                key = (playlist_id, start, stop, title)

                if key not in seen:
                    # Copy the entire programme before clearing its source.
                    copied = ET.fromstring(
                        ET.tostring(element, encoding="utf-8")
                    )

                    copied.set("channel", playlist_id)
                    output_root.append(copied)
                    seen.add(key)
                    added += 1

        # Clear only after processing the complete programme.
        element.clear()

    return added, skipped_empty_titles


def main():
    playlist_channels = read_playlist(PLAYLIST_FILE)
    playlist_ids = set(playlist_channels)

    name_index = build_playlist_name_index(playlist_channels)

    output_root = ET.Element("tv", {
        "source-info-name": "Automatic EPG Generator",
        "generator-info-name": "GitHub Actions XMLTV Generator",
    })

    seen_programmes = set()
    matched_playlist_ids = set()
    total_programmes = 0
    total_empty_titles = 0

    with tempfile.TemporaryDirectory() as temp_dir:
        for number, url in enumerate(EPG_SOURCES, start=1):
            xml_file = Path(temp_dir) / f"source_{number}.xml"

            try:
                download_source(url, xml_file)

                source_mapping = read_source_channel_mapping(
                    xml_file,
                    name_index,
                    playlist_ids,
                )

                if not source_mapping:
                    print("No playlist channels matched this source.")
                    continue

                matched_playlist_ids.update(source_mapping.values())

                print(f"Matched source channels: {len(source_mapping)}")

                added, empty_titles = extract_programmes(
                    xml_file,
                    source_mapping,
                    output_root,
                    seen_programmes,
                )

                total_programmes += added
                total_empty_titles += empty_titles

                print(f"Added programmes: {added}")
                print(f"Skipped programmes with empty titles: {empty_titles}")

            except Exception as error:
                print(f"ERROR processing {url}: {error}")
                # Continue with other sources if one source fails.
                continue

    # Create XMLTV channel entries only for channels with programmes.
    channels_with_programmes = {
        programme.get("channel")
        for programme in output_root.findall("programme")
    }

    for playlist_id in sorted(channels_with_programmes):
        channel = playlist_channels[playlist_id]
        channel_element = ET.SubElement(
            output_root,
            "channel",
            {"id": playlist_id},
        )

        display_name = channel["names"][0] if channel["names"] else playlist_id
        ET.SubElement(channel_element, "display-name").text = display_name

    # Sort programmes by start time.
    programmes = output_root.findall("programme")
    programmes.sort(key=lambda item: (
        item.get("start", ""),
        item.get("channel", ""),
    ))

    # Rebuild the root in a predictable order: channels, then programmes.
    final_root = ET.Element("tv", output_root.attrib)

    for channel in output_root.findall("channel"):
        final_root.append(channel)

    for programme in programmes:
        final_root.append(programme)

    if not programmes:
        raise RuntimeError(
            "No titled programmes matched your playlist. "
            "Existing epg1.xml has been left untouched."
        )

    if not channels_with_programmes:
        raise RuntimeError("No matched channels have programmes.")

    temporary_output = OUTPUT_FILE.with_suffix(".xml.tmp")

    ET.ElementTree(final_root).write(
        temporary_output,
        encoding="utf-8",
        xml_declaration=True,
    )

    # Validate the new file before replacing the old one.
    check_root = ET.parse(temporary_output).getroot()

    check_programmes = check_root.findall("programme")
    titled_programmes = [
        p for p in check_programmes
        if (p.findtext("title") or "").strip()
    ]

    if not titled_programmes:
        temporary_output.unlink(missing_ok=True)
        raise RuntimeError("Generated EPG contains no programme titles.")

    # Replace old output only after successful validation.
    os.replace(temporary_output, OUTPUT_FILE)

    print("\n========== EPG GENERATION COMPLETE ==========")
    print(f"Playlist channels: {len(playlist_channels)}")
    print(f"Matched channels with programmes: {len(channels_with_programmes)}")
    print(f"Programmes written: {len(titled_programmes)}")
    print(f"Empty titles skipped: {total_empty_titles}")
    print(f"Output: {OUTPUT_FILE}")
    print(f"Output size: {OUTPUT_FILE.stat().st_size:,} bytes")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"\nFATAL ERROR: {error}", file=sys.stderr)
        raise


