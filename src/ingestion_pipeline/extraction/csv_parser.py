import csv
import logging
from pathlib import Path

from ..processing.preflight import PreflightError

log = logging.getLogger("ingest.csv")
MAX_ROWS = 10_000
MAX_COLUMNS = 100
MAX_CELLS = 100_000
MAX_CELL_CHARS = 10_000
MAX_HTML_BYTES = 4 * 1024 * 1024

try:
    from charset_normalizer import from_path as _cn_from_path
    _HAS_CHARSET_NORMALIZER = True
except ImportError:
    _HAS_CHARSET_NORMALIZER = False


def _detect_encoding(file_path: Path) -> str:
    """Best-effort encoding detection.

    Unlike .xlsx (which declares its encoding as part of the OOXML/zip
    format), a .csv is just bytes - no header states what encoding was
    used to write it. Prefers charset-normalizer when available; falls
    back to utf-8-sig (handles a BOM if present, otherwise behaves like
    utf-8) if the library isn't installed or can't decide.
    """
    if _HAS_CHARSET_NORMALIZER:
        try:
            best = _cn_from_path(str(file_path)).best()
            if best is not None and best.encoding:
                return best.encoding
        except Exception as e:
            log.debug("charset-normalizer failed on %s: %s", file_path.name, e)
    return "utf-8-sig"


def _detect_delimiter(sample: str) -> str:
    """Sniff the delimiter from a text sample rather than assuming comma -
    semicolon- and tab-separated exports (e.g. from European locales or
    Excel's regional "CSV" export) are common."""
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        return dialect.delimiter
    except csv.Error:
        return ","


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _read_rows(file_path: Path, encoding: str, delimiter: str) -> list[list[str]]:
    # newline="" per the csv module docs - avoids mangling quoted fields
    # that contain embedded newlines/CRLFs.
    with open(file_path, "r", encoding=encoding, errors="replace", newline="") as f:
        rows = []
        cells = 0
        for row in csv.reader(f, delimiter=delimiter):
            if len(rows) >= MAX_ROWS or len(row) > MAX_COLUMNS:
                raise PreflightError("csv_limit", "CSV dimensions exceed the supported limit")
            cells += len(row)
            if cells > MAX_CELLS or any(len(cell) > MAX_CELL_CHARS for cell in row):
                raise PreflightError("csv_limit", "CSV content exceeds the supported limit")
            rows.append(row)
        return rows


def parse_csv(file_path: str, image_out_dir: str = "./extracted_images") -> dict:
    """Parse a .csv file into the unified block schema.

    A single table block is produced (CSV has no concept of multiple
    sheets, unlike .xlsx/.xls). Encoding and delimiter are both detected
    rather than assumed, since CSVs commonly arrive in varied encodings
    and delimiter conventions depending on their source.

    Rows are read and rendered directly via the stdlib csv module rather
    than pandas.read_csv - many real-world "CSV" exports (quotations,
    invoices, letterhead documents saved out of Excel) are NOT a single
    uniform table: they mix a title row, a few key:value metadata rows,
    a genuine multi-column line-items table, and a totals/footer
    section, each with a different number of fields. pandas.read_csv
    requires one fixed column count for the whole file (inferred from
    the first row) and silently drops or misaligns every row that
    doesn't match it - so a file like that would come back missing most
    of its content. Rendering each row's actual cells directly, ragged
    widths and all, guarantees nothing is lost regardless of whether the
    file is a clean uniform table or a mixed-structure document.
    """
    file_path = Path(file_path)
    file_name = file_path.name
    result = {
        "source_file": str(file_path),
        "format": "csv",
        "blocks": [],
        "extracted_images": [],
    }

    if not file_path.is_file():
        raise PreflightError("missing_document", "CSV input is missing")

    encoding = _detect_encoding(file_path)

    try:
        with open(file_path, "r", encoding=encoding, errors="replace", newline="") as f:
            sample = f.read(4096)
        delimiter = _detect_delimiter(sample) if sample else ","
    except Exception as e:
        log.warning("Could not sniff encoding/delimiter for %s: %s - falling back to utf-8/comma", file_name, e)
        encoding = "utf-8-sig"
        delimiter = ","

    try:
        rows = _read_rows(file_path, encoding, delimiter)
    except PreflightError:
        raise
    except Exception as exc:
        raise PreflightError("corrupt_document", "CSV cannot be parsed") from exc

    # Drop fully-blank rows (common as visual separators in exported
    # spreadsheets) but keep every row with any real content, whatever
    # its column count.
    non_blank_rows = [r for r in rows if any(cell.strip() for cell in r)]

    if not non_blank_rows:
        log.info("CSV file %s is empty", file_name)
        return result

    html_rows = []
    html_bytes = 0
    for row in non_blank_rows:
        cells = "".join(f"<td>{_escape(cell)}</td>" for cell in row)
        line = f"<tr>{cells}</tr>"
        html_bytes += len(line.encode("utf-8"))
        if html_bytes > MAX_HTML_BYTES:
            raise PreflightError("output_limit", "CSV output exceeds the supported limit")
        html_rows.append(line)
    html_table = "<table>" + "".join(html_rows) + "</table>"

    result["blocks"].append({
        "block_label": "table",
        "content": html_table,
        "order": 0,
        "page": None,
        "confidence": 1.0,
        "source_file": file_name,
        "format": "csv",
    })

    return result
