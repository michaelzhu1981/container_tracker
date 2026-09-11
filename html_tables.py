"""Extract simple HTML tables without extra dependencies."""

from __future__ import annotations

from html.parser import HTMLParser


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[dict]]] = []
        self._table: list[list[dict]] | None = None
        self._row: list[dict] | None = None
        self._cell: dict | None = None
        self._capture = False
        self._bold_depth = 0
        self._style_stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_d = {k: (v or "") for k, v in attrs}
        style = attrs_d.get("style", "")
        cls = attrs_d.get("class", "")
        self._style_stack.append(f"{style} {cls}")
        if tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = {"text": "", "bold": False, "class": cls, "style": style}
            self._capture = True
            self._bold_depth = 0
        elif tag in {"b", "strong"} and self._capture:
            self._bold_depth += 1
            if self._cell is not None:
                self._cell["bold"] = True
        elif tag == "br" and self._capture and self._cell is not None:
            self._cell["text"] += " "

    def handle_endtag(self, tag: str) -> None:
        if tag in {"b", "strong"} and self._bold_depth:
            self._bold_depth -= 1
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            text = " ".join(self._cell["text"].split())
            style = f"{self._cell['style']} {self._cell['class']}".lower()
            if "bold" in style or "font-weight" in style and "700" in style:
                self._cell["bold"] = True
            if "actual" in style:
                self._cell["bold"] = True
            self._cell["text"] = text
            self._row.append(self._cell)
            self._cell = None
            self._capture = False
        elif tag == "tr" and self._row is not None and self._table is not None:
            if any(cell["text"] for cell in self._row):
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            self.tables.append(self._table)
            self._table = None
        if self._style_stack:
            self._style_stack.pop()

    def handle_data(self, data: str) -> None:
        if self._capture and self._cell is not None:
            self._cell["text"] += data
            if self._bold_depth:
                self._cell["bold"] = True


def parse_tables(html: str) -> list[list[list[dict]]]:
    parser = _TableParser()
    parser.feed(html)
    parser.close()
    return parser.tables
