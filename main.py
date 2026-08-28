import os
import re
import subprocess
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


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


def parse_size_from_headers(headers):
    content_range = headers.get("Content-Range")
    if content_range:
        match = re.search(r"/(\d+)$", content_range.strip())
        if match:
            return int(match.group(1))

    content_length = headers.get("Content-Length")
    if content_length and content_length.isdigit():
        return int(content_length)

    return None


def get_public_file_size(file_id, timeout=12, retries=1):
    url = f"https://drive.google.com/uc?export=view&id={file_id}"

    for _ in range(retries + 1):
        try:
            # Range request asks for only 1 byte, then uses Content-Range total size.
            request = Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Range": "bytes=0-0",
                },
            )
            with urlopen(request, timeout=timeout) as response:
                size = parse_size_from_headers(response.headers)
                if size is not None:
                    return size
        except (HTTPError, URLError, TimeoutError, OSError):
            continue

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


def collect_folder_data(folder_id, visited=None, progress_every=50):
    if visited is None:
        visited = set()
    if folder_id in visited:
        return 0, 0, defaultdict(int), []

    visited.add(folder_id)
    entries = parse_entries(fetch_folder_html(folder_id))

    total_files = 0
    total_folders = 0
    type_counts = defaultdict(int)
    file_ids = []

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
            sub_files, sub_folders, sub_counts, sub_file_ids = collect_folder_data(entry["resource_id"], visited, progress_every)
            total_files += sub_files
            total_folders += sub_folders + 1
            file_ids.extend(sub_file_ids)
            for key, value in sub_counts.items():
                type_counts[key] += value
            continue

        total_files += 1
        ext = entry["name"].rsplit(".", 1)[-1].lower() if "." in entry["name"] else "no_extension"
        type_counts[ext] += 1
        file_ids.append(entry["resource_id"])

    if entries:
        print_progress("scan complete", current=len(entries), total=len(entries), files=total_files, folders=total_folders)

    return total_files, total_folders, type_counts, file_ids


def sum_file_sizes_concurrently(file_ids, workers=64, progress_every=250):
    if not file_ids:
        return 0

    unique_file_ids = list(dict.fromkeys(file_ids))
    total_size = 0
    completed = 0
    total = len(unique_file_ids)

    print_progress("size fetch started", current=0, total=total)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(get_public_file_size, file_id) for file_id in unique_file_ids]
        for future in as_completed(futures):
            completed += 1
            file_size = future.result()
            if file_size is not None:
                total_size += file_size

            if completed % progress_every == 0 or completed == total:
                print_progress("fetching sizes", current=completed, total=total)

    print_progress("size fetch complete", current=total, total=total)
    return total_size


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
        total_files, total_folders, type_counts, file_ids = collect_folder_data(folder_id)
        worker_count = int(os.environ.get("SIZE_WORKERS", "64"))
        if worker_count < 1:
            worker_count = 1
        max_workers = min(128, len(file_ids) if file_ids else 1)
        worker_count = min(worker_count, max_workers)
        total_size = sum_file_sizes_concurrently(file_ids, workers=worker_count)
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
