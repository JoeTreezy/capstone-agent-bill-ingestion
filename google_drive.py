"""Google Drive integration: lists bill PDFs in the watched folder and
downloads their content. Replaces Retool's listBillFilesInFolder and the
download-per-file loop inside downloadEachNewFile.

Uses a service account (not a per-user OAuth flow) -- the right choice for
a backend service with no human present to click through a consent screen.
The service account needs the Drive folder shared with it (its email address
is in the downloaded JSON key file, under "client_email") for read access.
"""
from __future__ import annotations

import base64
import io
from typing import Optional

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from config import settings

_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
_drive_service = None


def get_drive_service():
    global _drive_service
    if _drive_service is None:
        credentials = service_account.Credentials.from_service_account_file(
            settings.drive_service_account_json_path, scopes=_SCOPES
        )
        _drive_service = build("drive", "v3", credentials=credentials)
    return _drive_service


def list_files_in_folder(folder_id: Optional[str] = None) -> list[dict]:
    """Returns [{"file_id", "file_name", "drive_link"}, ...] for every file
    currently in the watched folder (not yet filtered against what's already
    been processed -- that comparison happens in poll_and_process.py against
    the extracted_files table)."""
    service = get_drive_service()
    folder_id = folder_id or settings.drive_folder_id
    if not folder_id:
        raise ValueError("drive_folder_id is not configured")

    files = []
    page_token = None
    while True:
        response = service.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            fields="nextPageToken, files(id, name, webViewLink)",
            pageToken=page_token,
        ).execute()
        for f in response.get("files", []):
            files.append({"file_id": f["id"], "file_name": f["name"], "drive_link": f.get("webViewLink", "")})
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return files


def download_file_content_base64(file_id: str) -> str:
    """Downloads one file's bytes and returns them base64-encoded, ready for
    extraction.extract_bill_data (which expects an already-encoded string,
    not raw bytes -- see the content_base64 naming in extraction.py)."""
    service = get_drive_service()
    request = service.files().get_media(fileId=file_id)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


def download_files(file_listings: list[dict]) -> list[dict]:
    """Takes [{"file_id", "file_name", "drive_link"}, ...] (from list_files_in_folder,
    already filtered to just the new ones) and returns the same list with
    content_base64 added to each -- the exact shape extraction.run_extraction expects."""
    result = []
    for f in file_listings:
        result.append({**f, "content_base64": download_file_content_base64(f["file_id"])})
    return result