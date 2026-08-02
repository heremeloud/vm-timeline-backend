#!/usr/bin/env python3
"""Migrate referenced post media to YYYYMM/lowercase-name keys.

Dry-run is the default. --apply copies objects, verifies them, backs up SQLite,
and updates post URLs. Source objects are intentionally never deleted.
"""

import argparse
import json
import os
import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv


BACKEND_DIR = Path(__file__).resolve().parents[1]
DB_PATH = BACKEND_DIR / "vm-social.db"


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")[:60]


def media_kind(platform: str, content_type: str | None) -> str:
    if content_type == "story":
        return "igs"
    if content_type == "broadcast":
        return "bc"
    return "ig" if platform == "ig" else platform


def destination_configs() -> dict[str, dict[str, str]]:
    configs = {}
    for label, bucket_var, url_var in (
        ("primary", "R2_VM_VIDEO_BUCKET", "R2_VM_VIDEO_PUBLIC_URL"),
        ("related", "R2_RELATED_BUCKET", "R2_RELATED_PUBLIC_URL"),
        ("vimmy", "R2_VIMMY_BUCKET", "R2_VIMMY_PUBLIC_URL"),
    ):
        bucket = (os.getenv(bucket_var) or "").strip()
        public_url = (os.getenv(url_var) or "").strip().rstrip("/")
        if not bucket or not public_url:
            raise RuntimeError(f"Missing {bucket_var} or {url_var}")
        configs[urlparse(public_url).netloc] = {
            "label": label,
            "bucket": bucket,
            "public_url": public_url,
        }
    return configs


def load_mappings(conn: sqlite3.Connection, configs: dict[str, dict[str, str]]) -> list[dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT p.id, p.platform, p.content_type, p.posted_at, p.media_url,
               p.media_urls_json, a.name AS author
        FROM post p
        JOIN author a ON a.id = p.author_id
        WHERE p.media_url LIKE '%r2.dev%'
           OR p.media_urls_json LIKE '%r2.dev%'
        ORDER BY p.posted_at, p.id
        """
    ).fetchall()

    mappings = []
    for row in rows:
        kind = media_kind(row["platform"], row["content_type"])
        date = datetime.strptime(row["posted_at"], "%Y-%m-%d")
        author = slug(row["author"])
        candidates = []
        if row["media_url"]:
            candidates.append(row["media_url"])

        try:
            media_items = json.loads(row["media_urls_json"] or "[]")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Post {row['id']} contains invalid media JSON") from exc
        for item in media_items:
            url = item.get("url") if isinstance(item, dict) else item
            if url:
                candidates.append(url)

        sequence = 0
        for old_url in candidates:
            parsed = urlparse(old_url)
            config = configs.get(parsed.netloc)
            if not config:
                continue
            sequence += 1
            extension = Path(parsed.path).suffix.lower()
            if not extension:
                raise RuntimeError(f"No extension for post {row['id']}: {old_url}")
            filename = f"{author}-{date:%Y%m%d}-{kind}-{sequence:02d}{extension}"
            new_key = f"{date:%Y%m}/{filename}"
            mappings.append({
                "post_id": row["id"],
                "old_url": old_url,
                "old_key": unquote(parsed.path.lstrip("/")),
                "new_url": f"{config['public_url']}/{new_key}",
                "new_key": new_key,
                "bucket": config["bucket"],
                "destination": config["label"],
            })
    return mappings


def validate_mappings(mappings: list[dict]) -> None:
    duplicate_targets = [key for key, count in Counter((m["bucket"], m["new_key"]) for m in mappings).items() if count > 1]
    if duplicate_targets:
        preview = ", ".join(f"{bucket}/{key}" for bucket, key in duplicate_targets[:10])
        raise RuntimeError(f"Target key collisions detected: {preview}")


def r2_client():
    account_id = (os.getenv("R2_ACCOUNT_ID") or "").strip()
    access_key = (os.getenv("R2_ACCESS_KEY_ID") or "").strip()
    secret_key = (os.getenv("R2_SECRET_ACCESS_KEY") or "").strip()
    if not account_id or not access_key or not secret_key:
        raise RuntimeError("Missing R2 credentials")
    return boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        region_name="auto",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
    )


def copy_and_verify(client, mapping: dict) -> None:
    try:
        source = client.head_object(Bucket=mapping["bucket"], Key=mapping["old_key"])
    except ClientError as exc:
        raise RuntimeError(f"Source object missing: {mapping['bucket']}/{mapping['old_key']}") from exc

    try:
        target = client.head_object(Bucket=mapping["bucket"], Key=mapping["new_key"])
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in {"404", "NoSuchKey", "NotFound"}:
            raise
        client.copy_object(
            Bucket=mapping["bucket"],
            Key=mapping["new_key"],
            CopySource={"Bucket": mapping["bucket"], "Key": mapping["old_key"]},
            MetadataDirective="COPY",
        )
        target = client.head_object(Bucket=mapping["bucket"], Key=mapping["new_key"])

    if source["ContentLength"] != target["ContentLength"]:
        raise RuntimeError(f"Size mismatch after copy: {mapping['new_key']}")


def update_database(conn: sqlite3.Connection, mappings: list[dict]) -> None:
    for mapping in mappings:
        conn.execute(
            "UPDATE post SET media_url = replace(media_url, ?, ?) WHERE id = ?",
            (mapping["old_url"], mapping["new_url"], mapping["post_id"]),
        )
        conn.execute(
            "UPDATE post SET media_urls_json = replace(media_urls_json, ?, ?) WHERE id = ?",
            (mapping["old_url"], mapping["new_url"], mapping["post_id"]),
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Copy objects and update the database")
    args = parser.parse_args()

    load_dotenv(BACKEND_DIR / ".env")
    configs = destination_configs()
    conn = sqlite3.connect(DB_PATH)
    mappings = load_mappings(conn, configs)
    validate_mappings(mappings)

    by_destination = Counter(mapping["destination"] for mapping in mappings)
    print(f"Planned migrations: {len(mappings)}")
    for destination, count in sorted(by_destination.items()):
        print(f"  {destination}: {count}")
    for mapping in mappings[:8]:
        print(f"  {mapping['old_key']} -> {mapping['new_key']}")
    if len(mappings) > 8:
        print(f"  ... and {len(mappings) - 8} more")

    if not args.apply:
        print("Dry run only. No R2 objects or database rows were changed.")
        return 0

    backup = DB_PATH.with_name(f"{DB_PATH.stem}.pre-r2-migration-{datetime.now():%Y%m%d-%H%M%S}{DB_PATH.suffix}")
    backup_conn = sqlite3.connect(backup)
    try:
        conn.backup(backup_conn)
    finally:
        backup_conn.close()
    print(f"Database backup: {backup}")

    client = r2_client()
    for index, mapping in enumerate(mappings, start=1):
        copy_and_verify(client, mapping)
        if index % 25 == 0 or index == len(mappings):
            print(f"Copied and verified {index}/{len(mappings)}")

    try:
        with conn:
            update_database(conn, mappings)
    except Exception:
        print(f"Database update failed. Restore from {backup}")
        raise

    print("Migration complete. Original R2 objects were retained.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
