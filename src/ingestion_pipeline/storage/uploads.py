"""Store untrusted uploads under generated names with strict byte budgets."""

import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from fastapi import HTTPException, UploadFile

from ..extraction.manager import ALL_SUPPORTED_EXTS

MAX_FILES = 8
MAX_FILE_BYTES = 15 * 1024 * 1024
MAX_REQUEST_BYTES = 30 * 1024 * 1024
READ_CHUNK = 64 * 1024
_UNSAFE = re.compile(r"[\x00-\x1f\x7f/\\]")


@dataclass(frozen=True)
class StoredUpload:
    document_id: str
    original_name: str
    path: Path
    size: int


def validate_name(name: str | None) -> tuple[str, str]:
    if (not isinstance(name, str) or not name or len(name) > 255
            or name in {".", ".."} or _UNSAFE.search(name)
            or Path(name).name != name or Path(name).is_absolute()):
        raise HTTPException(status_code=400, detail="Invalid document filename")
    suffix = Path(name).suffix.lower()
    if suffix not in ALL_SUPPORTED_EXTS:
        raise HTTPException(status_code=400, detail="Unsupported document format")
    return name, suffix


def _signature_ok(ext: str, prefix: bytes) -> bool:
    if ext == ".pdf":
        return prefix.startswith(b"%PDF-")
    if ext in {".jpg", ".jpeg"}:
        return prefix.startswith(b"\xff\xd8\xff")
    if ext == ".png":
        return prefix.startswith(b"\x89PNG\r\n\x1a\n")
    if ext == ".bmp":
        return prefix.startswith(b"BM")
    if ext in {".tif", ".tiff"}:
        return prefix.startswith((b"II*\x00", b"MM\x00*"))
    if ext == ".webp":
        return prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP"
    if ext in {".docx", ".xlsx"}:
        return (prefix.startswith(b"PK\x03\x04") or prefix.startswith(b"PK\x05\x06")
                or prefix.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"))
    if ext == ".xls":
        return prefix.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
    # HTML and CSV are text containers; reject NUL rather than guessing an encoding.
    return b"\x00" not in prefix


async def save_uploads(files: Sequence[UploadFile], directory: Path) -> list[StoredUpload]:
    if not 1 <= len(files) <= MAX_FILES:
        raise HTTPException(status_code=413, detail="Too many documents")
    names: set[str] = set()
    prepared: list[tuple[UploadFile, str, str]] = []
    for upload in files:
        name, ext = validate_name(upload.filename)
        if name.casefold() in names:
            raise HTTPException(status_code=400, detail="Duplicate document filename")
        names.add(name.casefold())
        prepared.append((upload, name, ext))

    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    saved: list[StoredUpload] = []
    total = 0
    try:
        for upload, name, ext in prepared:
            doc_id = uuid.uuid4().hex
            path = directory / f"{doc_id}{ext}"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path, flags, 0o600)
            size = 0
            prefix = b""
            try:
                with os.fdopen(fd, "wb") as output:
                    while chunk := await upload.read(READ_CHUNK):
                        size += len(chunk)
                        total += len(chunk)
                        if size > MAX_FILE_BYTES or total > MAX_REQUEST_BYTES:
                            raise HTTPException(status_code=413, detail="Document upload exceeds size limit")
                        if len(prefix) < 16:
                            prefix = (prefix + chunk)[:16]
                        output.write(chunk)
                if size == 0 or not _signature_ok(ext, prefix):
                    raise HTTPException(status_code=400, detail="Document content does not match its format")
            except BaseException:
                path.unlink(missing_ok=True)
                raise
            saved.append(StoredUpload(doc_id, name, path, size))
        return saved
    except BaseException:
        for item in saved:
            item.path.unlink(missing_ok=True)
        raise
