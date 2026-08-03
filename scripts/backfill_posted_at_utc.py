"""Backfill Post.posted_at_utc from existing social post URLs.

Dry-run by default. Pass --apply to update rows. Existing exact timestamps,
Instagram Stories, and broadcast messages are never changed.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


DB_PATH = Path(__file__).resolve().parents[1] / "vm-social.db"
BANGKOK = ZoneInfo("Asia/Bangkok")
X_EPOCH_MS = 1_288_834_974_657
IG_EPOCH_MS = 1_314_220_021_721
IG_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def decode_timestamp(platform: str, content_type: str, url: str) -> tuple[datetime, bool] | None:
    if not url or (platform == "ig" and content_type != "post"):
        return None

    if platform == "x":
        match = re.search(r"/status/(\d+)", url, re.IGNORECASE)
        if not match:
            return None
        epoch_ms = (int(match.group(1)) >> 22) + X_EPOCH_MS
        estimated = False
    elif platform == "tt":
        match = re.search(r"/video/(\d+)", url, re.IGNORECASE)
        if not match:
            return None
        epoch_ms = (int(match.group(1)) >> 32) * 1000
        estimated = False
    elif platform == "ig":
        match = re.search(r"/(?:p|reel|tv)/([A-Za-z0-9_-]+)", url, re.IGNORECASE)
        if not match:
            return None
        media_id = 0
        for character in match.group(1):
            media_id = media_id * 64 + IG_ALPHABET.index(character)
        epoch_ms = (media_id >> 23) + IG_EPOCH_MS
        estimated = True
    else:
        return None

    value = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
    if value.year < 2006 or value > datetime.now(timezone.utc):
        return None
    return value, estimated


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write proposed timestamps to the database")
    args = parser.parse_args()

    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        """
        SELECT id, parent_id, platform, coalesce(content_type, 'post') AS content_type,
               external_url, posted_at, posted_at_utc
        FROM post
        ORDER BY id
        """
    ).fetchall()

    proposals: dict[int, tuple[str, bool, str]] = {}
    mismatches: list[tuple[int, str, str]] = []
    skipped_existing = 0
    unsupported = 0

    for row in rows:
        if row["posted_at_utc"]:
            skipped_existing += 1
            continue
        decoded = decode_timestamp(row["platform"], row["content_type"], row["external_url"] or "")
        if not decoded:
            unsupported += 1
            continue
        timestamp, estimated = decoded
        utc_value = timestamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        bangkok_date = timestamp.astimezone(BANGKOK).date().isoformat()
        if row["posted_at"] and row["posted_at"] != bangkok_date:
            mismatches.append((row["id"], row["posted_at"], bangkok_date))
        proposals[row["id"]] = (utc_value, estimated, bangkok_date)

    # Do not create an impossible exact ordering for child X posts.
    rows_by_id = {row["id"]: row for row in rows}
    invalid_replies = []
    for post_id, (utc_value, _, _) in list(proposals.items()):
        row = rows_by_id[post_id]
        if row["parent_id"] is None:
            continue
        parent = rows_by_id.get(row["parent_id"])
        parent_utc = proposals.get(
            row["parent_id"],
            (parent["posted_at_utc"] if parent else None, False, ""),
        )[0]
        if parent_utc and utc_value <= parent_utc:
            invalid_replies.append(post_id)
            del proposals[post_id]

    print(f"Database: {DB_PATH}")
    print(f"Proposed updates: {len(proposals)}")
    print(f"Already populated: {skipped_existing}")
    print(f"Unsupported/skipped: {unsupported}")
    print(f"Stored-date vs Bangkok-date mismatches: {len(mismatches)}")
    for post_id, stored, detected in mismatches[:20]:
        print(f"  post {post_id}: stored {stored}, detected Bangkok date {detected}")
    if len(mismatches) > 20:
        print(f"  ...and {len(mismatches) - 20} more")
    print(f"Replies rejected by parent-time validation: {len(invalid_replies)}")

    invalid_text_replies = connection.execute(
        """
        SELECT text_row.id, parent.posted_at
        FROM posttext AS text_row
        JOIN post AS parent ON parent.id = text_row.post_id
        WHERE text_row.posted_at IS NOT NULL
          AND parent.posted_at IS NOT NULL
          AND text_row.posted_at < parent.posted_at
        """
    ).fetchall()
    print(f"Date-only replies to move to their parent's date: {len(invalid_text_replies)}")

    if not args.apply:
        print("Dry run only; pass --apply to write these updates.")
        return

    with connection:
        connection.executemany(
            """
            UPDATE post
            SET posted_at_utc = ?, posted_at_is_estimated = ?, posted_at = ?
            WHERE id = ? AND posted_at_utc IS NULL
            """,
            [
                (utc_value, int(estimated), bangkok_date, post_id)
                for post_id, (utc_value, estimated, bangkok_date) in proposals.items()
            ],
        )
        connection.executemany(
            "UPDATE posttext SET posted_at = ? WHERE id = ?",
            [(row["posted_at"], row["id"]) for row in invalid_text_replies],
        )
    print(f"Applied {len(proposals)} updates.")
    print(f"Corrected {len(invalid_text_replies)} date-only reply dates.")


if __name__ == "__main__":
    main()
