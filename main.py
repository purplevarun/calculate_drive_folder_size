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
    last_error = ""

    for attempt in range(2):
        proc = subprocess.run(
            [
                "curl",
                "-sS",
                "-L",
                "--http1.1",
                "--connect-timeout",
                "20",
                "--max-time",
                "180",
                "-A",
                "Mozilla/5.0",
                url,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            html = proc.stdout
            if html and "<title>Sign in - Google Accounts</title>" not in html:
                return html
            last_error = "public access denied or empty response"
            break

        last_error = proc.stderr.strip() or "unknown curl error"

    raise RuntimeError(f"curl failed: {last_error}")


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

        file_match = re.search(r"/file/d/([A-Za-z0-9_-]+)", href)
        folder_match = re.search(r"/folders/([A-Za-z0-9_-]+)", href)

        entries.append({
            "id": item_id,
            "name": name,
            "is_folder": bool(folder_match),
            "resource_id": file_match.group(1) if file_match else (folder_match.group(1) if folder_match else item_id),
            "href": href,
        })

    return entries


def get_public_file_size(file_id):
    url = f"https://drive.google.com/uc?export=view&id={file_id}"
    proc = subprocess.run(
        [
            "curl",
            "-sSL",
            "--connect-timeout",
            "20",
            "--max-time",
            "90",
            "-A",
            "Mozilla/5.0",
            "-o",
            "/dev/null",
            "-w",
            "%{size_download}",
            url,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None

    stdout = proc.stdout.strip()
    if not stdout:
        return None

    try:
        return int(stdout)
    except ValueError:
        return None


def print_progress(message, current=None, total=None, files=None, folders=None):
    parts = [f"[progress] {message}"]
    if current is not None and total is not None:
        parts.append(f"{current}/{total}")
    if files is not None:
        parts.append(f"{files} files")
    if folders is not None:
        parts.append(f"{folders} folders")
    print(" | ".join(parts), flush=True)


def analyze_folder(folder_id, visited=None, progress_every=50):
    if visited is None:
        visited = set()
    if folder_id in visited:
        return 0, 0, defaultdict(int), 0

    visited.add(folder_id)
    entries = parse_entries(fetch_folder_html(folder_id))

    total_files = 0
    total_folders = 0
    total_size = 0
    type_counts = defaultdict(int)

    if entries:
        print_progress("scan started", current=0, total=len(entries), files=0, folders=0)

    for index, entry in enumerate(entries, start=1):
        should_log = index % progress_every == 0 or index == len(entries)
        if should_log:
            print_progress(
                "scanning",
                current=index,
                total=len(entries),
                files=total_files,
                folders=total_folders,
            )

        if entry["is_folder"]:
            sub_files, sub_folders, sub_counts, sub_size = analyze_folder(entry["resource_id"], visited, progress_every)
            total_files += sub_files
            total_folders += sub_folders + 1
            total_size += sub_size
            for key, value in sub_counts.items():
                type_counts[key] += value
            continue

        total_files += 1
        ext = entry["name"].rsplit(".", 1)[-1].lower() if "." in entry["name"] else "no_extension"
        type_counts[ext] += 1

        file_size = get_public_file_size(entry["resource_id"])
        if file_size is not None:
            total_size += file_size

    if entries:
        print_progress("scan complete", current=len(entries), total=len(entries), files=total_files, folders=total_folders)

    return total_files, total_folders, type_counts, total_size


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
        total_files, total_folders, type_counts, total_size = analyze_folder(folder_id)
    except RuntimeError as exc:
        print(f"Network/API error while reading folder: {exc}")
        sys.exit(1)
    except PermissionError as exc:
        print(exc)
        sys.exit(1)

    print("\n===== DRIVE FOLDER ANALYSIS =====")
    print(f"Total Folders: {total_folders}")
    print(f"Total Files: {total_files}")
    print(f"Total Size: {total_size} bytes")
    print(f"Total Size: {round(total_size / (1024**3), 2)} GB")
    print("\nFile Types:")
    for file_type, count in sorted(type_counts.items()):
        print(f"{file_type}: {count}")
