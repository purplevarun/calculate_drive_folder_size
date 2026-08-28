import os
import re
import subprocess
import sys
from collections import defaultdict


def load_url_from_env_or_cli(argv):
    cli_url = argv[1] if len(argv) > 1 else None

    if cli_url:
        return cli_url

    env_path = ".env"
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as env_file:
            for line in env_file:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("URL="):
                    env_url = line.split("=", 1)[1].strip().strip('"\'')
                    if env_url:
                        return env_url

    raise ValueError("No URL provided. Pass a CLI argument or set URL= in .env")


def extract_folder_id(url):
    match = re.search(r"/folders/([A-Za-z0-9_-]+)", url)
    if match:
        return match.group(1)
    raise ValueError("Invalid Google Drive folder URL")


def fetch_folder_html(folder_id):
    url = f"https://drive.google.com/embeddedfolderview?id={folder_id}#list"
    proc = subprocess.run(
        ["curl", "-sS", "-L", "--http1.1", "--max-time", "30", url],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"curl failed: {proc.stderr.strip() or 'unknown error'}")

    html = proc.stdout
    if not html:
        raise RuntimeError("Received empty response from Google Drive.")

    if "<title>Sign in - Google Accounts</title>" in html:
        raise PermissionError("This folder is not publicly accessible. Please share it publicly and try again.")

    return html


def parse_entries(html):
    pattern = re.compile(
        r'<div class="flip-entry" id="entry-(?P<id>[A-Za-z0-9_-]+)".*?'
        r'<a href="(?P<href>[^"]+)"[^>]*>.*?'
        r'<div class="flip-entry-title">(?P<name>.*?)</div>',
        re.S,
    )

    entries = []
    seen = set()

    for match in pattern.finditer(html):
        item_id = match.group("id")
        if item_id in seen:
            continue
        seen.add(item_id)

        href = match.group("href")
        name = re.sub(r"\s+", " ", match.group("name")).strip()
        entries.append({
            "id": item_id,
            "name": name,
            "is_folder": "/folders/" in href,
            "href": href,
        })

    return entries


def analyze_folder(folder_id, visited=None):
    if visited is None:
        visited = set()
    if folder_id in visited:
        return 0, 0, defaultdict(int)

    visited.add(folder_id)
    entries = parse_entries(fetch_folder_html(folder_id))

    total_files = 0
    total_folders = 0
    type_counts = defaultdict(int)

    for entry in entries:
        if entry["is_folder"]:
            folder_match = re.search(r"/folders/([A-Za-z0-9_-]+)", entry["href"])
            sub_id = folder_match.group(1) if folder_match else entry["id"]
            sub_files, sub_folders, sub_counts = analyze_folder(sub_id, visited)
            total_files += sub_files
            total_folders += sub_folders + 1
            for key, value in sub_counts.items():
                type_counts[key] += value
            continue

        total_files += 1
        ext = entry["name"].rsplit(".", 1)[-1].lower() if "." in entry["name"] else "no_extension"
        type_counts[ext] += 1

    return total_files, total_folders, type_counts


if __name__ == "__main__":
    try:
        url = load_url_from_env_or_cli(sys.argv)
    except ValueError as exc:
        print(exc)
        sys.exit(1)

    try:
        folder_id = extract_folder_id(url)
    except ValueError as exc:
        print(exc)
        sys.exit(1)

    try:
        total_files, total_folders, type_counts = analyze_folder(folder_id)
    except RuntimeError as exc:
        print(f"Network/API error while reading folder: {exc}")
        sys.exit(1)
    except PermissionError as exc:
        print(exc)
        sys.exit(1)

    print("\n===== DRIVE FOLDER ANALYSIS =====")
    print(f"Total Folders: {total_folders}")
    print(f"Total Files: {total_files}")
    print("\nFile Types:")
    for file_type, count in sorted(type_counts.items()):
        print(f"{file_type}: {count}")
