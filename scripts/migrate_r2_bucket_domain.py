#!/usr/bin/env python3
"""Verify copied R2 keys and replace an old public bucket domain in SQLite."""

import argparse
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

import boto3
from dotenv import load_dotenv


BACKEND_DIR = Path(__file__).resolve().parents[1]
DB_PATH = BACKEND_DIR / "vm-social.db"


def text_columns(conn: sqlite3.Connection):
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    for (table,) in tables:
        for column in conn.execute(f'PRAGMA table_info("{table}")').fetchall():
            if "CHAR" in (column[2] or "").upper() or "TEXT" in (column[2] or "").upper():
                yield table, column[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--old-public-url", required=True)
    parser.add_argument("--destination", choices=("primary", "related", "vimmy"), default="primary")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    load_dotenv(BACKEND_DIR / ".env")
    old_base = args.old_public_url.rstrip("/")
    bucket_var, public_url_var = {
        "primary": ("R2_VM_BUCKET", "R2_VM_PUBLIC_URL"),
        "related": ("R2_RELATED_BUCKET", "R2_RELATED_PUBLIC_URL"),
        "vimmy": ("R2_VIMMY_BUCKET", "R2_VIMMY_PUBLIC_URL"),
    }[args.destination]
    new_base = os.environ[public_url_var].rstrip("/")
    bucket = os.environ[bucket_var]
    if old_base == new_base:
        raise RuntimeError("Old and new public URLs are identical")

    conn = sqlite3.connect(DB_PATH)
    columns = list(text_columns(conn))
    references = []
    for table, column in columns:
        rows = conn.execute(
            f'SELECT rowid, "{column}" FROM "{table}" WHERE "{column}" LIKE ?',
            (f"%{old_base}%",),
        ).fetchall()
        references.extend((table, column, rowid, value) for rowid, value in rows)

    keys = set()
    for _, _, _, value in references:
        start = 0
        while True:
            start = value.find(old_base, start)
            if start < 0:
                break
            path_start = start + len(old_base)
            path_end = len(value)
            for marker in ('"', "'", " ", "\\n", "\\r", "]", "}", ","):
                found = value.find(marker, path_start)
                if found >= 0:
                    path_end = min(path_end, found)
            url = value[start:path_end].rstrip("\\")
            parsed = urlparse(url)
            if parsed.path:
                keys.add(unquote(parsed.path.lstrip("/")))
            start = path_end

    client = boto3.client(
        "s3",
        endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        region_name="auto",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
    )
    available = set()
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket):
        available.update(item["Key"] for item in page.get("Contents", []))

    missing = sorted(keys - available)
    redirects = {}
    for old_key in missing:
        match = re.fullmatch(r"(.+)-(igs|bc)-(\d{6})(?:-(\d+))?(\.[^.]+)", old_key)
        if not match:
            continue
        author, kind, short_date, sequence, extension = match.groups()
        full_date = f"20{short_date}"
        candidate = f"{full_date[:6]}/{author}-20{short_date}-{kind}-{int(sequence or 1):02d}{extension.lower()}"
        if candidate in available:
            redirects[old_key] = candidate
    unresolved = [key for key in missing if key not in redirects]
    print(f"Database rows containing old domain: {len(references)}")
    print(f"Unique referenced object keys: {len(keys)}")
    print(f"Keys present directly in {bucket}: {len(keys) - len(missing)}/{len(keys)}")
    for old_key, new_key in redirects.items():
        print(f"Resolved renamed key: {old_key} -> {new_key}")
    if unresolved:
        print("Missing keys:")
        for key in unresolved:
            print(f"  {key}")
        raise RuntimeError("Database was not changed because migrated objects are missing")

    if not args.apply:
        print("Dry run only. Database was not changed.")
        return 0

    backup = DB_PATH.with_name(f"{DB_PATH.stem}.pre-bucket-domain-{datetime.now():%Y%m%d-%H%M%S}{DB_PATH.suffix}")
    backup_conn = sqlite3.connect(backup)
    try:
        conn.backup(backup_conn)
    finally:
        backup_conn.close()

    with conn:
        for table, column in columns:
            for old_key, new_key in redirects.items():
                conn.execute(
                    f'UPDATE "{table}" SET "{column}" = replace("{column}", ?, ?) WHERE "{column}" LIKE ?',
                    (f"{old_base}/{old_key}", f"{new_base}/{new_key}", f"%{old_base}/{old_key}%"),
                )
            conn.execute(
                f'UPDATE "{table}" SET "{column}" = replace("{column}", ?, ?) WHERE "{column}" LIKE ?',
                (old_base, new_base, f"%{old_base}%"),
            )
    print(f"Database backup: {backup}")
    print(f"Replaced {old_base} with {new_base}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
