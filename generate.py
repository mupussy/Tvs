
import os
from pathlib import Path

# Input and output files
source = Path("list1.m3u8")
output = Path("generated.m3u8")

# Read credentials from GitHub Secrets
username = os.environ.get("IPTV_USERNAME")
password = os.environ.get("IPTV_PASSWORD")

if not username or not password:
    raise RuntimeError("Missing IPTV credentials in GitHub Secrets")

if not source.exists():
    raise FileNotFoundError("list1.m3u8 was not found")

# Read original playlist
content = source.read_text(encoding="utf-8")

# Replace placeholders throughout the playlist
content = content.replace("{{USERNAME}}", username)
content = content.replace("{{PASSWORD}}", password)

# Check that no placeholders remain
if "{{USERNAME}}" in content or "{{PASSWORD}}" in content:
    raise RuntimeError("Some credentials were not replaced")

# Save generated playlist
output.write_text(content, encoding="utf-8")

print("Playlist generated successfully.")
print(f"Output: {output}")
