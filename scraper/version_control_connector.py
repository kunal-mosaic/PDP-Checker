"""
version_control_connector.py — reads the NPD Version Control Sheet (Man Matters tab),
finds packaging Drive folder links for each SKU in a product, and downloads packaging
files locally so Claude Vision can analyse them.

Uses the same Google Application Default Credentials as sheets_connector.py.
"""

import io
import os
import re
import time
from pathlib import Path
from typing import List, Optional
from dataclasses import dataclass, field

import gspread
import google.auth
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from utils.config_loader import load_config
from utils.logger import get_logger

log = get_logger("version_control_connector")

VERSION_CONTROL_SHEET_ID = "1FKAYZ8D28j0QA5gN6VFPaseoEG5uA__iPYhfo8JrCT0"
SHEET_TAB = "Man Matters"
DOWNLOAD_DIR = Path("outputs") / "packaging_cache"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/cloud-platform",
]


@dataclass
class SKUPackaging:
    sku_name: str
    version: str          # e.g. "V7"
    drive_folder_url: str
    local_files: List[Path] = field(default_factory=list)  # downloaded packaging files


@dataclass
class PackagingData:
    product_name: str
    skus: List[SKUPackaging] = field(default_factory=list)
    error: Optional[str] = None


# ── Auth ───────────────────────────────────────────────────────────────────────

def _get_credentials():
    credentials, _ = google.auth.default(scopes=SCOPES)
    return credentials


def _get_sheets_client(credentials) -> gspread.Client:
    return gspread.authorize(credentials)


def _get_drive_service(credentials):
    return build("drive", "v3", credentials=credentials)


# ── Sheet reading ──────────────────────────────────────────────────────────────

def _load_vc_rows() -> List[dict]:
    """Load all rows from the Man Matters tab as list of dicts keyed by header."""
    try:
        creds = _get_credentials()
        client = _get_sheets_client(creds)
        sheet = client.open_by_key(VERSION_CONTROL_SHEET_ID)
        ws = sheet.worksheet(SHEET_TAB)
        all_rows = ws.get_all_values()
        if not all_rows:
            return []
        headers = all_rows[0]
        result = []
        for row in all_rows[1:]:
            padded = row + [""] * (len(headers) - len(row))
            result.append(dict(zip(headers, padded)))
        return result
    except Exception as e:
        log.error(f"Failed to load Version Control Sheet: {e}")
        return []


def _find_latest_drive_link(row: dict) -> tuple[str, str]:
    """
    Scan a row right-to-left to find the rightmost non-empty Drive folder link.
    Returns (version_label, drive_url) or ("", "") if none found.
    """
    # Version columns in order V1..V7 (plus any beyond)
    version_cols = [k for k in row.keys() if re.match(r"V\d+\s*(Link)?", k, re.I)]
    # Sort by version number descending so we check latest first
    def _ver_num(col_name):
        m = re.search(r"(\d+)", col_name)
        return int(m.group(1)) if m else 0
    version_cols_sorted = sorted(version_cols, key=_ver_num, reverse=True)

    for col in version_cols_sorted:
        val = row.get(col, "").strip()
        if "drive.google.com" in val:
            ver_label = re.sub(r"\s*(Link)?", "", col, flags=re.I).strip()
            return ver_label, val

    return "", ""


def _fuzzy_match(sku_name: str, row_product: str) -> bool:
    """Case-insensitive substring match after normalising whitespace."""
    a = " ".join(sku_name.lower().split())
    b = " ".join(row_product.lower().split())
    return a in b or b in a


# ── Drive download ─────────────────────────────────────────────────────────────

def _extract_folder_id(drive_url: str) -> Optional[str]:
    """Extract Google Drive folder ID from a Drive URL."""
    patterns = [
        r"folders/([a-zA-Z0-9_-]+)",
        r"id=([a-zA-Z0-9_-]+)",
    ]
    for pattern in patterns:
        m = re.search(pattern, drive_url)
        if m:
            return m.group(1)
    return None


def _download_folder(drive_service, folder_id: str, dest_dir: Path, file_filter: str = "") -> List[Path]:
    """
    List files in a Google Drive folder and download images + PDFs.
    If file_filter is set, only files whose names contain that substring (case-insensitive) are downloaded.
    Returns list of local file paths.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    downloaded = []

    try:
        results = drive_service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="files(id, name, mimeType)",
            pageSize=20,
        ).execute()
        files = results.get("files", [])
    except Exception as e:
        log.warning(f"Could not list Drive folder {folder_id}: {e}")
        return []

    allowed_mimes = {
        "image/jpeg", "image/png", "image/webp",
        "application/pdf",
        "image/gif",
    }

    for f in files:
        mime = f.get("mimeType", "")
        name = f.get("name", "file")
        fid = f["id"]

        if mime not in allowed_mimes:
            continue

        if file_filter and file_filter.lower() not in name.lower():
            log.info(f"      Skipping (filter '{file_filter}'): {name}")
            continue

        local_path = dest_dir / name
        if local_path.exists():
            log.info(f"      Cache hit: {name}")
            downloaded.append(local_path)
            continue

        try:
            request = drive_service.files().get_media(fileId=fid)
            buf = io.BytesIO()
            downloader = MediaIoBaseDownload(buf, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            local_path.write_bytes(buf.getvalue())
            downloaded.append(local_path)
            log.info(f"      Downloaded: {name}")
        except Exception as e:
            log.warning(f"Could not download {name}: {e}")

    return downloaded


# ── Main entry point ───────────────────────────────────────────────────────────

def fetch_packaging(
    product_name: str,
    sku_names: List[str],
    file_filters: dict = None,
    drive_folder_overrides: dict = None,
) -> PackagingData:
    """
    Main entry point called from main.py.
    Looks up each SKU in the Version Control Sheet and downloads its latest packaging.

    Args:
        product_name: e.g. "Shilajit Gummies"
        sku_names: list of SKU names from config.yaml version_control_skus field
        file_filters: optional dict {sku_name: filename_substring} to filter Drive files
        drive_folder_overrides: optional dict {sku_name: drive_folder_url} — bypasses the
            version control sheet lookup for that SKU and uses the provided URL directly.
            Use when the Drive folder is known (e.g. from an email) but not yet in the sheet.
    """
    if not sku_names:
        return PackagingData(product_name=product_name, error="No SKUs configured")

    log.info(f"Fetching packaging for {product_name} ({len(sku_names)} SKUs)")

    # Only load the sheet if at least one SKU doesn't have a drive override
    overrides = drive_folder_overrides or {}
    needs_sheet = any(sku not in overrides for sku in sku_names)
    rows = _load_vc_rows() if needs_sheet else []
    if needs_sheet and not rows:
        return PackagingData(product_name=product_name, error="Could not load Version Control Sheet")

    try:
        creds = _get_credentials()
        drive_service = _get_drive_service(creds)
    except Exception as e:
        return PackagingData(product_name=product_name, error=f"Drive auth failed: {e}")

    result = PackagingData(product_name=product_name)

    for sku_name in sku_names:
        # Check for direct Drive folder override first
        if sku_name in overrides:
            drive_url = overrides[sku_name]
            version = "latest"
            log.info(f"      {sku_name} → override: {drive_url[:60]}")
        else:
            # Find matching row in version control sheet
            matching_row = None
            for row in rows:
                if _fuzzy_match(sku_name, row.get("Product Name", "")):
                    matching_row = row
                    break

            if not matching_row:
                log.warning(f"      No row found in Version Control Sheet for: {sku_name}")
                result.skus.append(SKUPackaging(
                    sku_name=sku_name,
                    version="Not found",
                    drive_folder_url="",
                    local_files=[],
                ))
                continue

            version, drive_url = _find_latest_drive_link(matching_row)
        if not drive_url:
            log.warning(f"      No Drive link for: {sku_name}")
            result.skus.append(SKUPackaging(
                sku_name=sku_name,
                version="No link",
                drive_folder_url="",
                local_files=[],
            ))
            continue

        log.info(f"      {sku_name} → {version}: {drive_url[:60]}")

        folder_id = _extract_folder_id(drive_url)
        if not folder_id:
            log.warning(f"      Could not parse folder ID from: {drive_url}")
            result.skus.append(SKUPackaging(
                sku_name=sku_name,
                version=version,
                drive_folder_url=drive_url,
                local_files=[],
            ))
            continue

        # Safe download directory: product_slug/sku_slug/version
        product_slug = re.sub(r"[^\w]", "_", product_name.lower())
        sku_slug = re.sub(r"[^\w]", "_", sku_name.lower())[:40]
        dest = DOWNLOAD_DIR / product_slug / sku_slug / version
        file_filter = (file_filters or {}).get(sku_name, "")
        local_files = _download_folder(drive_service, folder_id, dest, file_filter=file_filter)

        result.skus.append(SKUPackaging(
            sku_name=sku_name,
            version=version,
            drive_folder_url=drive_url,
            local_files=local_files,
        ))

    log.info(f"Packaging fetch complete: {sum(len(s.local_files) for s in result.skus)} files across {len(result.skus)} SKUs")
    return result
