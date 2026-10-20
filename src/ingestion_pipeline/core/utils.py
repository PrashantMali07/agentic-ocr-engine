import difflib
import re


def normalize_text(text: str) -> str:
    """Lowercase and collapse whitespace so comparisons aren't thrown off
    by formatting differences that don't reflect real content loss."""
    if not isinstance(text, str):
        text = str(text)
    return re.sub(r"\s+", " ", text.lower()).strip()

def text_similarity(a: str, b: str) -> float:
    """0-1 similarity score. Order-sensitive (SequenceMatcher)."""
    a, b = normalize_text(a), normalize_text(b)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()

def word_coverage(extracted: str, original: str) -> float:
    """What fraction of the original's unique words also appear in the
    extracted text. Order-insensitive."""
    orig_words = set(normalize_text(original).split())
    if not orig_words:
        return 1.0
    ext_words = set(normalize_text(extracted).split())
    return len(orig_words & ext_words) / len(orig_words)

def _strip_html_tags(html_content: str) -> str:
    """Flatten an HTML table to plain space-separated text for comparison
    purposes only."""
    try:
        from bs4 import BeautifulSoup
        return BeautifulSoup(html_content, "lxml").get_text(separator=" ", strip=True)
    except Exception:
        return re.sub(r"<[^>]+>", " ", html_content)
