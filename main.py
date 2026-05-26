import os
import re
import sys
import pickle
from collections import defaultdict
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ['https://www.googleapis.com/auth/drive.readonly']


def extract_folder_id(url):
    match = re.search(r'folders/([a-zA-Z0-9_-]+)', url)
    if match:
        return match.group(1)
    else:
        raise ValueError("Invalid Google Drive folder URL")


def authenticate():
    creds = None

    if os.path.exists('token.pickle'):
        with open('token.pickle', 'rb') as token:
            creds = pickle.load(token)

    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(
            'credentials.json', SCOPES
        )
        creds = flow.run_local_server(port=0)

        with open('token.pickle', 'wb') as token:
            pickle.dump(creds, token)

    return build('drive', 'v3', credentials=creds)


def analyze_folder(service, folder_id):
    page_token = None

    total_files = 0
    total_folders = 0
    total_size = 0
    type_counts = defaultdict(int)

    while True:
        results = service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            spaces='drive',
            fields="nextPageToken, files(id, name, mimeType, size)",
            pageToken=page_token
        ).execute()

        items = results.get('files', [])

        for item in items:

            if item['mimeType'] == 'application/vnd.google-apps.folder':
                total_folders += 1

                # recurse into subfolder
                sub_files, sub_folders, sub_size, sub_types = analyze_folder(
                    service, item['id']
                )

                total_files += sub_files
                total_folders += sub_folders
                total_size += sub_size

                for k, v in sub_types.items():
                    type_counts[k] += v

            else:
                total_files += 1

                ext = os.path.splitext(item['name'])[1].lower().replace('.', '')
                if ext:
                    type_counts[ext] += 1
                else:
                    type_counts['no_extension'] += 1

                size = int(item.get('size', 0))
                total_size += size

        page_token = results.get('nextPageToken')
        if not page_token:
            break

    return total_files, total_folders, total_size, type_counts


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python main.py <GoogleDriveFolderURL>")
        sys.exit(1)

    url = sys.argv[1]
    folder_id = extract_folder_id(url)

    service = authenticate()

    total_files, total_folders, total_size, type_counts = analyze_folder(service, folder_id)

    print("\n===== DRIVE FOLDER ANALYSIS =====")
    print(f"Total Folders: {total_folders}")
    print(f"Total Files: {total_files}")

    print("\nFile Types:")
    for file_type, count in sorted(type_counts.items()):
        print(f"{file_type}: {count}")

    print(f"\nTotal Size: {round(total_size / (1024**3), 2)} GB")
