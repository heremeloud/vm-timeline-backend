import os
import re
from pathlib import Path
from urllib.parse import quote

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from middleware.auth import require_admin


router = APIRouter(prefix="/media", tags=["Media"])
ALLOWED_CONTENT_PREFIXES = ("image/", "video/")
DEFAULT_MAX_UPLOAD_BYTES = 200 * 1024 * 1024


def _destinations() -> dict[str, dict[str, str | None]]:
    return {
        "primary": {
            "bucket": os.getenv("R2_VM_VIDEO_BUCKET"),
            "public_url": os.getenv("R2_VM_VIDEO_PUBLIC_URL"),
        },
        "related": {
            "bucket": os.getenv("R2_RELATED_BUCKET"),
            "public_url": os.getenv("R2_RELATED_PUBLIC_URL"),
        },
        "vimmy": {
            "bucket": os.getenv("R2_VIMMY_BUCKET"),
            "public_url": os.getenv("R2_VIMMY_PUBLIC_URL"),
        },
    }


def _required_env(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"R2 is not configured: missing {name}",
        )
    return value


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")[:60]


def _object_key(author: str, posted_at: str, media_type: str, sequence: int, filename: str | None) -> str:
    author_slug = _slug(author)
    if not author_slug:
        raise HTTPException(status_code=400, detail="A valid author is required")

    date_match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", posted_at.strip())
    if not date_match:
        raise HTTPException(status_code=400, detail="Posted date must use YYYY-MM-DD")
    year, month, day = date_match.groups()

    type_slug = _slug(media_type)
    if type_slug not in {"igs", "bc", "ig", "x", "tt"}:
        raise HTTPException(status_code=400, detail="Unknown media type")
    if sequence < 1 or sequence > 999:
        raise HTTPException(status_code=400, detail="Sequence must be between 1 and 999")

    extension = re.sub(r"[^a-zA-Z0-9.]", "", Path(filename or "").suffix.lower())[:10]
    if not extension:
        raise HTTPException(status_code=400, detail="The uploaded file needs a file extension")

    date_digits = f"{year}{month}{day}"
    generated_name = f"{author_slug}-{date_digits}-{type_slug}-{sequence:02d}{extension}"
    return f"{year}{month}/{generated_name}"


def _file_size(file: UploadFile) -> int:
    file.file.seek(0, os.SEEK_END)
    size = file.file.tell()
    file.file.seek(0)
    return size


@router.post("/upload", dependencies=[Depends(require_admin)])
def upload_media(
    destination: str = Form(...),
    author: str = Form(...),
    posted_at: str = Form(...),
    media_type: str = Form(...),
    sequence: int = Form(...),
    file: UploadFile = File(...),
):
    config = _destinations().get(destination)
    if not config:
        raise HTTPException(status_code=400, detail="Unknown upload destination")

    bucket = (config.get("bucket") or "").strip()
    public_url = (config.get("public_url") or "").strip().rstrip("/")
    if not bucket or not public_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"R2 destination '{destination}' is not configured",
        )

    content_type = (file.content_type or "").lower()
    if not content_type.startswith(ALLOWED_CONTENT_PREFIXES):
        raise HTTPException(status_code=415, detail="Only image and video uploads are allowed")

    size = _file_size(file)
    max_bytes = int(os.getenv("R2_MAX_UPLOAD_BYTES", str(DEFAULT_MAX_UPLOAD_BYTES)))
    if size <= 0:
        raise HTTPException(status_code=400, detail="The uploaded file is empty")
    if size > max_bytes:
        max_mb = max_bytes // (1024 * 1024)
        raise HTTPException(status_code=413, detail=f"File exceeds the {max_mb} MB upload limit")

    object_key = _object_key(author, posted_at, media_type, sequence, file.filename)
    account_id = _required_env("R2_ACCOUNT_ID")
    client = boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        region_name="auto",
        aws_access_key_id=_required_env("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=_required_env("R2_SECRET_ACCESS_KEY"),
    )

    try:
        try:
            client.head_object(Bucket=bucket, Key=object_key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") not in {"404", "NoSuchKey", "NotFound"}:
                raise
        else:
            raise HTTPException(status_code=409, detail=f"A file named '{Path(object_key).name}' already exists")

        client.upload_fileobj(
            file.file,
            bucket,
            object_key,
            ExtraArgs={
                "ContentType": content_type,
                "CacheControl": "public, max-age=31536000, immutable",
            },
        )
    except HTTPException:
        raise
    except (BotoCoreError, ClientError) as exc:
        raise HTTPException(status_code=502, detail="R2 upload failed") from exc

    return {
        "url": f"{public_url}/{quote(object_key, safe='/')}",
        "key": object_key,
        "destination": destination,
        "size": size,
        "content_type": content_type,
    }
