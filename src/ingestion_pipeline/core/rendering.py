import html
import io


def escape_md_text(text: str) -> str:
    """Escape HTML-significant characters in OCR'd/extracted text before
    writing it into markdown."""
    if not isinstance(text, str):
        text = str(text)
    return html.escape(text, quote=False)

def html_table_to_markdown(html_content: str) -> str:
    """Convert an HTML table to a markdown table for readable, citation-
    friendly MD output. Falls back to raw HTML if conversion fails."""
    try:
        import pandas as pd
        dfs = pd.read_html(io.StringIO(html_content))
        return "\n\n".join(df.fillna("").to_markdown(index=False) for df in dfs)
    except Exception:
        # Use a logging utility if available, otherwise a simple print or pass
        return html_content

def render_plain_text(attachments_meta: list[dict], blocks: list[dict]) -> str:
    """Produce clean, unstructured text -- no markdown syntax, no citation
    tags, no HTML -- suitable for NLP preprocessing."""
    lines = []

    by_file: dict[str, list[dict]] = {}
    for b in blocks:
        by_file.setdefault(b["source_file"], []).append(b)

    for att in attachments_meta:
        fname = att["file"]
        file_blocks = sorted(by_file.get(fname, []), key=lambda b: (b.get("page") or 0, b["order"]))

        for b in file_blocks:
            label = b["block_label"]
            content = b.get("content", "")

            if not isinstance(content, str):
                content = str(content)

            if label == "image":
                continue

            elif label == "table":
                try:
                    import pandas as pd
                    dfs = pd.read_html(io.StringIO(content))
                    for df in dfs:
                        for _, row in df.iterrows():
                            lines.append(" ".join(str(v) for v in row.values if str(v) != "nan"))
                except Exception:
                    pass

            else:
                cleaned = content.strip()
                if cleaned:
                    lines.append(cleaned)

    return "\n\n".join(lines)

def render_markdown(email_id: str, attachments_meta: list[dict], blocks: list[dict]) -> str:
    lines = [f"# Email: {email_id}\n"]

    by_file: dict[str, list[dict]] = {}
    for b in blocks:
        by_file.setdefault(b["source_file"], []).append(b)

    for att in attachments_meta:
        fname = att["file"]
        lines.append(f"## Source: {fname} ({att['format']})\n")
        file_blocks = sorted(by_file.get(fname, []), key=lambda b: (b.get("page") or 0, b["order"]))

        for b in file_blocks:
            citation = f"*(source: {fname}" + (f", page {b['page']}" if b.get("page") else "") + ")*"
            label = b["block_label"]

            if label == "heading":
                lines.append(f"### {escape_md_text(b['content'])}  {citation}\n")
            elif label == "table":
                md_table = html_table_to_markdown(b["content"])
                lines.append(f"{md_table}\n\n{citation}\n")
            elif label == "image":
                lines.append(f"![image]({b['content']})  {citation}\n")
            else:
                lines.append(f"{escape_md_text(b['content'])}  {citation}\n")

        lines.append("")

    return "\n".join(lines)
