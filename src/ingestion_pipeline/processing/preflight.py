"""Bound container metadata before untrusted documents reach full parsers."""

import math
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

MAX_INPUT_BYTES = 15 * 1024 * 1024
MAX_PDF_PAGES = 50
MAX_PDF_SIDE_POINTS = 4000
MAX_IMAGE_PIXELS = 20_000_000
MAX_IMAGE_SIDE = 10_000
MAX_ZIP_MEMBERS = 256
MAX_ZIP_EXPANDED_BYTES = 32 * 1024 * 1024
MAX_ZIP_MEMBER_BYTES = 16 * 1024 * 1024
MAX_ZIP_RATIO = 100
MAX_TEXT_BYTES = 5 * 1024 * 1024

SUPPORTED = {
    ".pdf", ".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp",
    ".docx", ".xlsx", ".xls", ".html", ".htm", ".csv",
}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}


class PreflightError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class PreflightInfo:
    format: str
    pages: int | None
    size: int


def _check_pdf(path: Path) -> int:
    import pymupdf

    try:
        with pymupdf.open(path) as doc:
            if doc.needs_pass:
                raise PreflightError("password_required", "Document requires a password")
            pages = doc.page_count
            if pages < 1 or pages > MAX_PDF_PAGES:
                raise PreflightError("page_limit", "PDF page count exceeds the supported limit")
            for page in doc:
                width, height = page.rect.width, page.rect.height
                if (not math.isfinite(width) or not math.isfinite(height)
                        or width <= 0 or height <= 0 or width > MAX_PDF_SIDE_POINTS
                        or height > MAX_PDF_SIDE_POINTS):
                    raise PreflightError("page_dimensions", "PDF page dimensions exceed the supported limit")
            return pages
    except PreflightError:
        raise
    except Exception as exc:
        raise PreflightError("corrupt_document", "PDF cannot be opened") from exc


def _check_image(path: Path) -> int:
    from PIL import Image

    try:
        with Image.open(path) as image:
            width, height = image.size
            if (width < 1 or height < 1 or width > MAX_IMAGE_SIDE or height > MAX_IMAGE_SIDE
                    or width * height > MAX_IMAGE_PIXELS):
                raise PreflightError("image_dimensions", "Image dimensions exceed the supported limit")
            if getattr(image, "n_frames", 1) != 1:
                raise PreflightError("image_frames", "Multi-frame images are not supported")
            image.verify()
            return 1
    except PreflightError:
        raise
    except Exception as exc:
        raise PreflightError("corrupt_document", "Image cannot be opened") from exc


def _check_zip(path: Path, required: str) -> None:
    with path.open("rb") as source:
        is_ole = source.read(8) == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
        if is_ole:
            # OLE is a container format, not proof that its contents are encrypted.
            try:
                import msoffcrypto

                source.seek(0)
                encrypted = msoffcrypto.OfficeFile(source).is_encrypted()
            except Exception as exc:
                raise PreflightError("corrupt_document", "Document encryption metadata cannot be read") from exc
            if encrypted:
                raise PreflightError("password_required", "Document requires a password")
            raise PreflightError("corrupt_document", "Document is not a valid OOXML archive")
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if not members or len(members) > MAX_ZIP_MEMBERS:
                raise PreflightError("archive_limit", "Document archive has too many entries")
            expanded = 0
            names = set()
            for member in members:
                name = member.filename
                parts = PurePosixPath(name).parts
                if (not parts or name.startswith("/") or "\\" in name or ".." in parts
                        or ":" in parts[0] or stat.S_ISLNK(member.external_attr >> 16)):
                    raise PreflightError("archive_path", "Document archive contains an unsafe entry")
                if member.flag_bits & 1:
                    raise PreflightError("password_required", "Document requires a password")
                if member.file_size > MAX_ZIP_MEMBER_BYTES:
                    raise PreflightError("archive_limit", "Document archive entry is too large")
                expanded += member.file_size
                if expanded > MAX_ZIP_EXPANDED_BYTES:
                    raise PreflightError("archive_limit", "Document archive expands beyond the supported limit")
                if member.file_size > 1024 and member.file_size / max(member.compress_size, 1) > MAX_ZIP_RATIO:
                    raise PreflightError("archive_ratio", "Document archive has excessive compression")
                names.add(name)
            if required not in names:
                raise PreflightError("corrupt_document", "Document archive is missing required content")
    except PreflightError:
        raise
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise PreflightError("corrupt_document", "Document archive cannot be opened") from exc


def check_document(path: Path) -> PreflightInfo:
    """Inspect bounded metadata; full parsing still runs in the supervised worker."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext not in SUPPORTED:
        raise PreflightError("unsupported_format", "Document format is unsupported")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise PreflightError("missing_document", "Document input is missing") from exc
    if size < 1 or size > MAX_INPUT_BYTES:
        raise PreflightError("file_limit", "Document size exceeds the supported limit")
    pages = None
    if ext == ".pdf":
        pages = _check_pdf(path)
    elif ext in IMAGE_EXTS:
        pages = _check_image(path)
    elif ext in {".docx", ".xlsx"}:
        _check_zip(path, "word/document.xml" if ext == ".docx" else "xl/workbook.xml")
    elif ext in {".csv", ".html", ".htm"}:
        if size > MAX_TEXT_BYTES:
            raise PreflightError("text_limit", "Text document exceeds the supported limit")
        with path.open("rb") as stream:
            if b"\x00" in stream.read():
                raise PreflightError("corrupt_document", "Text document contains binary data")
    return PreflightInfo(format=ext.lstrip("."), pages=pages, size=size)
