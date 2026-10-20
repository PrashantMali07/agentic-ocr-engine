import logging
from html import escape
from pathlib import Path

from ..core.safe_html import sanitize_tables
from ..processing.preflight import PreflightError

log = logging.getLogger("ingest.native")
MAX_NATIVE_BLOCKS = 10_000
MAX_NATIVE_CONTENT_CHARS = 4 * 1024 * 1024
MAX_DOCX_IMAGES = 32

def parse_docx(file_path: str, image_out_dir: str = "./extracted_images") -> dict:
    """Parse a .docx file into the unified block schema, preserving
    paragraph/table/image order as they appear in the document body."""
    from docx import Document
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    file_path = Path(file_path)
    image_dir = Path(image_out_dir)
    image_dir.mkdir(parents=True, exist_ok=True)

    doc = Document(str(file_path))
    blocks = []
    extracted_images = []
    order = 0

    def iter_block_items(document):
        parent_elm = document.element.body
        for child in parent_elm.iterchildren():
            if child.tag == qn("w:p"):
                yield Paragraph(child, document)
            elif child.tag == qn("w:tbl"):
                yield Table(child, document)

    def extract_images_from_paragraph(paragraph, doc_obj):
        paths = []
        for run in paragraph.runs:
            drawing_elements = run._element.findall(
                ".//{http://schemas.openxmlformats.org/drawingml/2006/main}blip"
            )
            for blip in drawing_elements:
                embed_id = blip.get(qn("r:embed"))
                if not embed_id:
                    continue
                if len(extracted_images) + len(paths) >= MAX_DOCX_IMAGES:
                    raise PreflightError("document_limit", "Document has too many embedded images")
                try:
                    image_part = doc_obj.part.related_parts[embed_id]
                    img_name = f"{file_path.stem}_img{len(extracted_images) + len(paths) + 1}{Path(image_part.partname).suffix}"
                    img_path = image_dir / img_name
                    img_path.write_bytes(image_part.blob)
                    paths.append(str(img_path))
                except PreflightError:
                    raise
                except Exception as e:
                    log.warning("Could not extract image in %s: %s", file_path.name, e)
        return paths

    for item in iter_block_items(doc):
        if order >= MAX_NATIVE_BLOCKS:
            raise PreflightError("document_limit", "Document has too many content blocks")
        if isinstance(item, Paragraph):
            text = item.text.strip()
            images = extract_images_from_paragraph(item, doc)
            for img_path in images:
                if len(extracted_images) >= MAX_DOCX_IMAGES:
                    raise PreflightError("document_limit", "Document has too many embedded images")
                extracted_images.append(img_path)
                blocks.append({
                    "block_label": "image", "content": img_path,
                    "order": order, "page": None, "confidence": 1.0,
                })
                order += 1
            if text:
                is_heading = item.style.name.lower().startswith("heading") if item.style else False
                blocks.append({
                    "block_label": "heading" if is_heading else "text",
                    "content": text, "order": order, "page": None, "confidence": 1.0,
                })
                order += 1

        elif isinstance(item, Table):
            rows_html = ["<table>"]
            if len(item.rows) > 10_000:
                raise PreflightError("document_limit", "Document table has too many rows")
            for row_idx, row in enumerate(item.rows):
                if len(row.cells) > 100:
                    raise PreflightError("document_limit", "Document table has too many columns")
                tag = "th" if row_idx == 0 else "td"
                cells = "".join(f"<{tag}>{escape(cell.text.strip())}</{tag}>" for cell in row.cells)
                rows_html.append(f"<tr>{cells}</tr>")
            rows_html.append("</table>")
            html_table = "".join(rows_html)
            blocks.append({
                "block_label": "table", "content": html_table,
                "order": order, "page": None, "confidence": 1.0,
            })
            order += 1

    if sum(len(str(block["content"])) for block in blocks) > MAX_NATIVE_CONTENT_CHARS:
        raise PreflightError("output_limit", "Document output exceeds the supported limit")

    return {
        "source_file": str(file_path),
        "format": "docx",
        "blocks": blocks,
        "extracted_images": extracted_images,
    }

def parse_html(file_path: str, image_out_dir: str = "./extracted_images", base_url: str | None = None) -> dict:
    """Parse an .html file into the unified block schema."""
    from bs4 import BeautifulSoup

    file_path = Path(file_path)
    image_dir = Path(image_out_dir)
    image_dir.mkdir(parents=True, exist_ok=True)

    html_text = file_path.read_text(encoding="utf-8", errors="ignore")
    soup = BeautifulSoup(html_text, "lxml")

    blocks = []
    extracted_images = []
    order = 0

    body = soup.body or soup
    TEXT_TAGS = {"p", "li", "span"}
    HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
    BLOCK_TAGS = TEXT_TAGS | HEADING_TAGS

    for el in body.find_all(list(BLOCK_TAGS | {"table", "img"}), recursive=True):
        if order >= MAX_NATIVE_BLOCKS:
            raise PreflightError("document_limit", "HTML document has too many content blocks")
        if el.find_parent("table") is not None:
            continue
        if el.name in BLOCK_TAGS and el.find_parent(list(BLOCK_TAGS)) is not None:
            continue

        if el.name == "table":
            blocks.append({
                "block_label": "table", "content": sanitize_tables(str(el)),
                "order": order, "page": None, "confidence": 1.0,
            })
            order += 1

        elif el.name == "img":
            src = el.get("src", "")
            if not src:
                continue
            note = src if src.startswith("http") or base_url is None else f"{base_url.rstrip('/')}/{src.lstrip('/')}"
            blocks.append({
                "block_label": "image", "content": note,
                "order": order, "page": None, "confidence": 1.0,
            })
            extracted_images.append(note)
            order += 1

        elif el.name in HEADING_TAGS:
            text = el.get_text(strip=True)
            if text:
                blocks.append({
                    "block_label": "heading", "content": text,
                    "order": order, "page": None, "confidence": 1.0,
                })
                order += 1

        else:
            text = el.get_text(separator=" ", strip=True)
            if text and len(text) > 1:
                blocks.append({
                    "block_label": "text", "content": text,
                    "order": order, "page": None, "confidence": 1.0,
                })
                order += 1

    if sum(len(str(block["content"])) for block in blocks) > MAX_NATIVE_CONTENT_CHARS:
        raise PreflightError("output_limit", "HTML output exceeds the supported limit")

    return {
        "source_file": str(file_path),
        "format": "html",
        "blocks": blocks,
        "extracted_images": extracted_images,
    }

def parse_document(file_path: str, image_out_dir: str = "./extracted_images") -> dict:
    """Route to the correct parser based on file extension."""
    ext = Path(file_path).suffix.lower()
    if ext == ".docx":
        return parse_docx(file_path, image_out_dir)
    elif ext in (".html", ".htm"):
        return parse_html(file_path, image_out_dir)
    else:
        raise ValueError(f"Unsupported format for this module: {ext} (use OCR for pdf/image)")

def tables_to_csv(record: dict, out_dir: str) -> list[str]:
    """Convert every table block's HTML into a standalone CSV."""
    import io

    import pandas as pd

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(record["source_file"]).stem
    csv_paths = []

    table_idx = 0
    for block in record["blocks"]:
        if block["block_label"] != "table":
            continue
        table_idx += 1
        try:
            dfs = pd.read_html(io.StringIO(block["content"]))
        except Exception as e:
            log.warning("Could not parse table %d in %s: %s", table_idx, stem, e)
            continue
        for i, df in enumerate(dfs):
            csv_name = f"{stem}_table{table_idx}" + (f"_{i}" if len(dfs) > 1 else "") + ".csv"
            csv_path = out_dir / csv_name
            df.to_csv(csv_path, index=False)
            csv_paths.append(str(csv_path))
    return csv_paths
