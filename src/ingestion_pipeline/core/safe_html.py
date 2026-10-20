"""Render only inert table markup from untrusted document content."""

from html import escape

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

_TABLE_TAGS = {"table", "caption", "thead", "tbody", "tfoot", "tr", "th", "td", "br"}
_DROP_CONTENT = {"script", "style", "iframe", "object", "embed", "svg", "math", "template", "form"}


def sanitize_tables(markup: str) -> str:
    if not markup:
        return ""
    soup = BeautifulSoup(str(markup), "html.parser")

    def render(node) -> str:
        if isinstance(node, Comment):
            return ""
        if isinstance(node, NavigableString):
            return escape(str(node), quote=True)
        if not isinstance(node, Tag):
            return ""
        name = (node.name or "").lower()
        if name in _DROP_CONTENT:
            return ""
        children = "".join(render(child) for child in node.children)
        if name not in _TABLE_TAGS:
            return children
        if name == "br":
            return "<br>"
        return f"<{name}>{children}</{name}>"

    return "".join(render(table) for table in soup.find_all("table") if table.find_parent("table") is None)
