
import os
import re
import unicodedata
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict

PLAYLIST = "list1.m3u8"
OUTPUT = "epg1.xml"

SOURCES = [
    "https://www.open-epg.com/files/belgium.xml",
    "https://www.open-epg.com/files/czech3.xml",
    "https://www.open-epg.com/files/france.xml",
    "https://www.open-epg.com/files/italy1.xml",
    "https://www.open-epg.com/files/saudiarabia.xml",
    "https://www.open-epg.com/files/spain.xml",
    "https://www.open-epg.com/files/unitedkingdom.xml",
]


def normalize(name):
    name = unicodedata.normalize("NFKD", name)
    name = name.encode("ascii", "ignore").decode().lower()
    name = re.sub(r"^\s*[a-z]{2,3}\s*:\s*", "", name)
    name = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", name)
    name = re.sub(r"\b(720p|1080p|2160p|uhd|fhd|hd|sd)\b", " ", name)
    name = re.sub(r"[^a-z0-9]", "", name)
    return name


def get_playlist():
    channels = {}

    with open(PLAYLIST, encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            if not line.lstrip().upper().startswith("#EXTINF"):
                continue

            match = re.search(
                r'''tvg-id\s*=\s*["']([^"']+)["']''',
                line,
                re.I
            )

            if not match:
                continue

            tvg_id = match.group(1).strip()
            name = line.rsplit(",", 1)[-1].strip()

            if tvg_id and name:
                channels[tvg_id] = name

    return channels


def download(url, destination):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0"}
    )

    with urllib.request.urlopen(request, timeout=180) as response:
        with open(destination, "wb") as f:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)


def read_source_channels(filename):
    channels = {}

    context = ET.iterparse(filename, events=("start", "end"))
    _, root = next(context)

    for event, elem in context:
        if event != "end":
            continue

        if elem.tag == "channel":
            channel_id = elem.get("id")

            if channel_id:
                names = [
                    (child.text or "").strip()
                    for child in elem.findall("display-name")
                    if (child.text or "").strip()
                ]

                channels[channel_id] = {
                    "names": names,
                }

            elem.clear()
            root.clear()

    return channels


def main():
    print("Starting EPG generator...")

    if not os.path.exists(PLAYLIST):
        raise FileNotFoundError(PLAYLIST)

    playlist = get_playlist()

    if not playlist:
        raise RuntimeError("No playlist channels with tvg-id found.")

    print(f"Playlist channels with IDs: {len(playlist)}")

    source_channels = {}
    downloaded = []

    with tempfile.TemporaryDirectory() as folder:

        # Download all seven XMLTV sources.
        for index, url in enumerate(SOURCES, 1):
            filename = os.path.join(folder, f"source_{index}.xml")

            print(f"\nDownloading source {index}/{len(SOURCES)}")

            try:
                download(url, filename)
                downloaded.append(filename)
                print("Download successful.")

            except Exception as error:
                print(f"Download failed: {error}")

        if not downloaded:
            raise RuntimeError("All EPG downloads failed. Old EPG preserved.")

        # Read channel IDs and display names from every source.
        for filename in downloaded:
            print(f"Reading channels from {os.path.basename(filename)}")

            try:
                channels = read_source_channels(filename)
                source_channels.update(channels)

            except ET.ParseError as error:
                print(f"Invalid source XML: {error}")

        print(f"\nSource channel IDs found: {len(source_channels)}")

        # Create exact ID matches first.
        source_to_playlist = {}

        for source_id in source_channels:
            if source_id in playlist:
                source_to_playlist[source_id] = source_id

        print(f"Exact ID matches: {len(source_to_playlist)}")

        # Build a name index for unmatched playlist channels.
        name_index = defaultdict(list)

        for source_id, info in source_channels.items():
            for name in info["names"]:
                normalized = normalize(name)

                if normalized:
                    name_index[normalized].append(source_id)

        # Match unmatched playlist channels by exact normalized name.
        for playlist_id, playlist_name in playlist.items():

            if playlist_id in source_to_playlist.values():
                continue

            normalized = normalize(playlist_name)

            candidates = list(set(name_index.get(normalized, [])))

            if len(candidates) == 1:
                source_to_playlist[candidates[0]] = playlist_id

        # Create output XML.
        output_root = ET.Element("tv", {
            "source-info-name": "Open-EPG",
            "generator-info-name": "Custom Playlist EPG Generator"
        })

        matched_ids = set(source_to_playlist.values())

        for playlist_id in sorted(matched_ids):
            channel = ET.SubElement(
                output_root,
                "channel",
                {"id": playlist_id}
            )

            ET.SubElement(
                channel,
                "display-name"
            ).text = playlist[playlist_id]

        programme_keys = set()
        programme_count = 0

        # Extract programmes from every downloaded source.
        for filename in downloaded:

            print(f"\nExtracting programmes: {os.path.basename(filename)}")

            context = ET.iterparse(filename, events=("start", "end"))
            _, root = next(context)

            try:
                for event, elem in context:

                    if event != "end":
                        continue

                    if elem.tag == "programme":

                        source_id = elem.get("channel")
                        playlist_id = source_to_playlist.get(source_id)

                        if playlist_id:

                            title = (
                                elem.findtext("title") or ""
                            ).strip()

                            start = elem.get("start", "")
                            stop = elem.get("stop", "")

                            key = (
                                playlist_id,
                                start,
                                stop,
                                title
                            )

                            if key not in programme_keys:

                                programme = ET.fromstring(
                                    ET.tostring(elem)
                                )

                                # Rewrite source ID to your playlist ID.
                                programme.set("channel", playlist_id)

                                output_root.append(programme)
                                programme_keys.add(key)
                                programme_count += 1

                    elem.clear()
                    root.clear()

            except ET.ParseError as error:
                print(f"Source parsing error: {error}")

        # Check that we have useful programme data.
        if not programme_count:
            raise RuntimeError(
                "No matching programmes found. Old EPG preserved."
            )

        # Write to a temporary file first.
        output_temp = OUTPUT + ".tmp"

        tree = ET.ElementTree(output_root)

        try:
            ET.indent(tree, space="  ")
        except AttributeError:
            pass

        tree.write(
            output_temp,
            encoding="utf-8",
            xml_declaration=True
        )

        # Validate before replacing the existing EPG.
        ET.parse(output_temp)

        os.replace(output_temp, OUTPUT)

        # Final report.
        unmatched = set(playlist) - matched_ids

        print("\n========== EPG REPORT ==========")
        print(f"Playlist channels: {len(playlist)}")
        print(f"Matched channels: {len(matched_ids)}")
        print(f"Unmatched channels: {len(unmatched)}")
        print(f"Programmes: {programme_count}")
        print(f"Output: {OUTPUT}")
        print(f"Size: {os.path.getsize(OUTPUT):,} bytes")

        if unmatched:
            print("\nUNMATCHED CHANNELS:")

            for channel_id in sorted(unmatched):
                print(channel_id)

        print("\nEPG GENERATED SUCCESSFULLY!")


if __name__ == "__main__":
    main()
