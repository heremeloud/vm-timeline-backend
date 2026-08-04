import os
import re
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from middleware.auth import require_admin


router = APIRouter(prefix="/media", tags=["Media"])
ALLOWED_CONTENT_PREFIXES = ("image/", "video/")
DEFAULT_MAX_UPLOAD_BYTES = 200 * 1024 * 1024


class MediaDeleteRequest(BaseModel):
    url: str


def _destinations() -> dict[str, dict[str, str | None]]:
    return {
        "primary": {
            "bucket": os.getenv("R2_VM_BUCKET"),
            "public_url": os.getenv("R2_VM_PUBLIC_URL"),
        },
        "related": {
            "bucket": os.getenv("R2_RELATED_BUCKET"),
            "public_url": os.getenv("R2_RELATED_PUBLIC_URL"),
        },
        "vimmy": {
            "bucket": os.getenv("R2_VIMMY_BUCKET"),
            "public_url": os.getenv("R2_VIMMY_PUBLIC_URL"),
        },
        "test": {
            "bucket": os.getenv("R2_TEST_BUCKET"),
            "public_url": os.getenv("R2_TEST_PUBLIC_URL"),
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


def _object_key(
    author: str,
    posted_at: str,
    media_type: str,
    sequence: int,
    filename: str | None,
    custom_filename: str | None = None,
) -> str:
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

    if custom_filename and custom_filename.strip():
        requested_name = Path(custom_filename.strip()).name
        requested_extension = re.sub(r"[^a-zA-Z0-9.]", "", Path(requested_name).suffix.lower())[:10]
        requested_stem = Path(requested_name).stem if requested_extension else requested_name
        safe_stem = re.sub(r"[^a-zA-Z0-9._-]+", "-", requested_stem).strip("._-")[:120]
        if not safe_stem:
            raise HTTPException(status_code=400, detail="The custom filename needs letters or numbers")
        return f"{year}{month}/{safe_stem}{requested_extension or extension}"

    date_digits = f"{year}{month}{day}"
    generated_name = f"{author_slug}-{date_digits}-{type_slug}-{sequence:02d}{extension}"
    return f"{year}{month}/{generated_name}"


def _file_size(file: UploadFile) -> int:
    file.file.seek(0, os.SEEK_END)
    size = file.file.tell()
    file.file.seek(0)
    return size


def _r2_client():
    account_id = _required_env("R2_ACCOUNT_ID")
    return boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        region_name="auto",
        aws_access_key_id=_required_env("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=_required_env("R2_SECRET_ACCESS_KEY"),
    )


def _resolve_public_object(url: str) -> tuple[str, str, str]:
    target = urlsplit(url.strip())
    if target.scheme not in {"http", "https"} or not target.netloc:
        raise HTTPException(status_code=400, detail="A valid public media URL is required")

    for destination, config in _destinations().items():
        bucket = (config.get("bucket") or "").strip()
        public_url = (config.get("public_url") or "").strip().rstrip("/")
        if not bucket or not public_url:
            continue

        base = urlsplit(public_url)
        base_path = base.path.rstrip("/")
        object_prefix = f"{base_path}/"
        if target.scheme != base.scheme or target.netloc != base.netloc or not target.path.startswith(object_prefix):
            continue

        object_key = unquote(target.path[len(object_prefix):])
        if not object_key or object_key.startswith("/") or any(part in {"", ".", ".."} for part in object_key.split("/")):
            raise HTTPException(status_code=400, detail="The media URL does not contain a valid R2 object key")
        return destination, bucket, object_key

    raise HTTPException(status_code=400, detail="This URL does not match a configured R2 destination")


def _resolve_download_object(url: str) -> tuple[str, str, str]:
    try:
        return _resolve_public_object(url)
    except HTTPException as exc:
        if exc.status_code != 400:
            raise

    # Existing database rows may retain an older public r2.dev hostname after
    # a bucket is moved to a custom domain. Resolve those URLs by object key,
    # but only against buckets already configured for this application.
    target = urlsplit(url.strip())
    if target.scheme != "https" or not target.netloc.lower().endswith(".r2.dev"):
        raise HTTPException(status_code=400, detail="This URL does not match a configured R2 destination")

    object_key = unquote(target.path.lstrip("/"))
    if not object_key or any(part in {"", ".", ".."} for part in object_key.split("/")):
        raise HTTPException(status_code=400, detail="The media URL does not contain a valid R2 object key")

    client = _r2_client()
    for destination, config in _destinations().items():
        bucket = (config.get("bucket") or "").strip()
        if not bucket:
            continue
        try:
            client.head_object(Bucket=bucket, Key=object_key)
            return destination, bucket, object_key
        except ClientError as lookup_exc:
            if lookup_exc.response.get("Error", {}).get("Code") not in {"404", "NoSuchKey", "NotFound"}:
                raise HTTPException(status_code=502, detail="R2 download lookup failed") from lookup_exc
        except BotoCoreError as lookup_exc:
            raise HTTPException(status_code=502, detail="R2 download lookup failed") from lookup_exc

    raise HTTPException(status_code=404, detail="The R2 object was not found in a configured bucket")


@router.get("/download")
def download_media(url: str = Query(...)):
    _, bucket, object_key = _resolve_download_object(url)

    try:
        obj = _r2_client().get_object(Bucket=bucket, Key=object_key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
            raise HTTPException(status_code=404, detail="The R2 object was not found") from exc
        raise HTTPException(status_code=502, detail="R2 download failed") from exc
    except BotoCoreError as exc:
        raise HTTPException(status_code=502, detail="R2 download failed") from exc

    body = obj["Body"]
    filename = Path(object_key).name

    def stream_file():
        try:
            for chunk in body.iter_chunks(chunk_size=1024 * 1024):
                if chunk:
                    yield chunk
        finally:
            body.close()

    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
        "Cache-Control": "private, max-age=0",
    }
    if obj.get("ContentLength") is not None:
        headers["Content-Length"] = str(obj["ContentLength"])

    return StreamingResponse(
        stream_file(),
        media_type=obj.get("ContentType") or "application/octet-stream",
        headers=headers,
    )


@router.post("/upload", dependencies=[Depends(require_admin)])
def upload_media(
    destination: str = Form(...),
    author: str = Form(...),
    posted_at: str = Form(...),
    media_type: str = Form(...),
    sequence: int = Form(...),
    filename: str | None = Form(None),
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

    object_key = _object_key(author, posted_at, media_type, sequence, file.filename, filename)
    client = _r2_client()

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


@router.delete("/object", dependencies=[Depends(require_admin)])
def delete_media_object(payload: MediaDeleteRequest):
    destination, bucket, object_key = _resolve_public_object(payload.url)
    client = _r2_client()

    try:
        # R2/S3 deletion is idempotent: deleting an object that is already
        # absent still leaves us in the requested state. Avoid a separate
        # existence check so stale local URLs can be removed cleanly.
        client.delete_object(Bucket=bucket, Key=object_key)
    except ClientError as exc:
        raise HTTPException(status_code=502, detail="R2 deletion failed") from exc
    except BotoCoreError as exc:
        raise HTTPException(status_code=502, detail="R2 deletion failed") from exc

    return {"deleted": True, "destination": destination, "key": object_key}
