#!/usr/bin/env python3
"""List legacy R2 objects that were not referenced by the migration database."""

import json
import os
import re
import sqlite3
from pathlib import Path
from urllib.parse import unquote, urlparse

import boto3
from dotenv import load_dotenv


BACKEND_DIR = Path(__file__).resolve().parents[1]


def referenced_keys(db_path: Path, public_host: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT media_url, media_urls_json FROM post WHERE media_url LIKE '%r2.dev%' OR media_urls_json LIKE '%r2.dev%'"
    ).fetchall()
    keys = set()
    for media_url, media_json in rows:
        urls = [media_url] if media_url else []
        try:
            items = json.loads(media_json or "[]")
        except json.JSONDecodeError:
            items = []
        for item in items:
            url = item.get("url") if isinstance(item, dict) else item
            if url:
                urls.append(url)
        for url in urls:
            parsed = urlparse(url)
            if parsed.netloc == public_host:
                keys.add(unquote(parsed.path.lstrip("/")))
    conn.close()
    return keys


def bucket_keys(client, bucket: str) -> set[str]:
    keys = set()
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket):
        keys.update(item["Key"] for item in page.get("Contents", []))
    return keys


def main() -> int:
    load_dotenv(BACKEND_DIR / ".env")
    backups = sorted(BACKEND_DIR.glob("vm-social.pre-r2-migration-*.db"))
    if not backups:
        raise RuntimeError("No pre-migration database backup found")
    backup = backups[-1]

    account_id = os.environ["R2_ACCOUNT_ID"]
    client = boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        region_name="auto",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
    )

    for label, bucket_var, url_var in (
        ("vm-video", "R2_VM_VIDEO_BUCKET", "R2_VM_VIDEO_PUBLIC_URL"),
        ("vm-video-related", "R2_RELATED_BUCKET", "R2_RELATED_PUBLIC_URL"),
        ("vimmy-video", "R2_VIMMY_BUCKET", "R2_VIMMY_PUBLIC_URL"),
    ):
        bucket = os.environ[bucket_var]
        host = urlparse(os.environ[url_var]).netloc
        referenced = referenced_keys(backup, host)
        all_keys = bucket_keys(client, bucket)
        legacy = {key for key in all_keys if not re.match(r"^\d{6}/", key)}
        unmatched = sorted(legacy - referenced)

        print(f"{label}: {len(legacy)} legacy objects, {len(referenced)} referenced, {len(unmatched)} unmatched")
        for key in unmatched:
            print(f"  {key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
