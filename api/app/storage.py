"""Audio files on local disk under STORAGE_DIR at {workspace_id}/{episode_id}.{ext}."""

# shortcut: local disk storage, single API server; move to S3-compatible storage before running 2+ instances  # noqa: E501

import hashlib
import hmac
import os
import time
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Query, UploadFile
from fastapi.responses import FileResponse

from app.config import get_settings
from app.errors import AppError

MAX_UPLOAD_BYTES = 500 * 1024 * 1024
CHUNK = 1024 * 1024
URL_TTL_SECONDS = 600
MEDIA_TYPES = {"mp3": "audio/mpeg", "m4a": "audio/mp4", "wav": "audio/wav", "mp4": "video/mp4"}
ALLOWED_UPLOAD_MIME = {
    "audio/mpeg",
    "audio/mp4",
    "audio/x-m4a",
    "audio/wav",
    "audio/x-wav",
    "audio/wave",
    "video/mp4",
}


def _canonical_uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def object_path(workspace_id: uuid.UUID | str, episode_id: uuid.UUID | str, ext: str) -> str:
    rel = f"{workspace_id}/{episode_id}.{ext.lower()}"
    validate_path(rel)
    return rel


def validate_path(rel: str) -> tuple[str, str, str]:
    """Return (workspace_id, episode_id, ext) or raise ValueError for anything else."""
    parts = rel.split("/")
    if len(parts) != 2:
        raise ValueError("path must be {workspace_id}/{episode_id}.{ext}")
    folder, filename = parts
    stem, _, ext = filename.partition(".")
    if not _canonical_uuid(folder) or not _canonical_uuid(stem) or ext not in MEDIA_TYPES:
        raise ValueError("path must be {workspace_id}/{episode_id}.{ext}")
    return folder, stem, ext


def absolute(rel: str) -> Path:
    validate_path(rel)
    return Path(get_settings().STORAGE_DIR).resolve() / rel


async def save_upload(file: UploadFile, rel: str, max_bytes: int = MAX_UPLOAD_BYTES) -> int:
    """Copy an upload to disk in 1 MB chunks, enforcing the cap; returns bytes written.

    shortcut: Starlette spools the multipart body to a temp file before the route runs;
    switch to a raw-body streaming PUT if temp-disk use for oversized uploads matters.
    """
    ext = rel.rsplit(".", 1)[-1]
    if (file.content_type or "") not in ALLOWED_UPLOAD_MIME:
        raise AppError(422, "validation_error", f"unsupported media type {file.content_type}")
    name_ext = (file.filename or "").rsplit(".", 1)[-1].lower()
    if name_ext != ext:
        raise AppError(422, "validation_error", f"file must be a .{ext} file")
    dest = absolute(rel)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    written = 0
    try:
        with part.open("wb") as out:
            while chunk := await file.read(CHUNK):
                written += len(chunk)
                if written > max_bytes:
                    raise AppError(413, "file_too_large", f"max {max_bytes // CHUNK} MB")
                out.write(chunk)
        os.replace(part, dest)
    finally:
        part.unlink(missing_ok=True)
    return written


def _sig(rel: str, exp: int) -> str:
    key = get_settings().FILE_URL_SECRET.encode()
    return hmac.new(key, f"{rel}\n{exp}".encode(), hashlib.sha256).hexdigest()


def signed_url(rel: str, ttl: int = URL_TTL_SECONDS, now: float | None = None) -> str:
    validate_path(rel)
    exp = int((now if now is not None else time.time()) + ttl)
    return f"/api/v1/media/{rel}?exp={exp}&sig={_sig(rel, exp)}"


def verify_signature(rel: str, exp: int, sig: str, now: float | None = None) -> bool:
    if exp < (now if now is not None else time.time()):
        return False
    return hmac.compare_digest(_sig(rel, exp), sig)


router = APIRouter(tags=["media"])


@router.get("/media/{workspace_id}/{filename}")
async def media(
    workspace_id: str,
    filename: str,
    exp: Annotated[int, Query()],
    sig: Annotated[str, Query(max_length=64)],
) -> FileResponse:
    rel = f"{workspace_id}/{filename}"
    try:
        _, _, ext = validate_path(rel)
    except ValueError:
        raise AppError(404, "not_found", "File not found") from None
    if not verify_signature(rel, exp, sig):
        raise AppError(403, "forbidden", "Invalid or expired link")
    path = absolute(rel)
    if not path.is_file():
        raise AppError(404, "not_found", "File not found")
    # Starlette's FileResponse answers Range requests with 206 (needed for <audio> seeking)
    return FileResponse(path, media_type=MEDIA_TYPES[ext])
