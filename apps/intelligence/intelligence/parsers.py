"""Bounded, offline parsers for corpus inputs."""
from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

from defusedxml import ElementTree as SafeET
from pypdf import PdfReader

MAX_TEXT_CHARS = 2_000_000
MAX_PDF_BYTES = 20_000_000
MAX_PDF_PAGES = 100
MAX_PDF_BASE64_CHARS = ((MAX_PDF_BYTES + 2) // 3) * 4


@dataclass(frozen=True)
class ParsedItem:
    title: str
    text: str
    published_at: str | None = None
    uri: str | None = None


@dataclass(frozen=True)
class ParseResult:
    text: str
    items: tuple[ParsedItem, ...] = ()
    warnings: tuple[str, ...] = ()


class _TextExtractor(HTMLParser):
    HIDDEN = {"script", "style", "noscript", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in self.HIDDEN:
            self.hidden += 1
        elif tag.lower() in {"p", "div", "br", "li", "h1", "h2", "h3", "tr"} and not self.hidden:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self.HIDDEN and self.hidden:
            self.hidden -= 1
        elif tag.lower() in {"p", "div", "li", "h1", "h2", "h3", "tr"} and not self.hidden:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def _clean(text: str) -> str:
    return re.sub(r"[ \t\xa0]+", " ", re.sub(r"\n\s*\n+", "\n", text)).strip()


def _bounded_text(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("content must be a string")
    if len(value) > MAX_TEXT_CHARS:
        raise ValueError(f"text exceeds {MAX_TEXT_CHARS} characters")
    return value


def parse_content(content: str, content_type: str) -> ParseResult:
    """Parse supported content without fetching URLs or executing embedded content."""
    if content_type == "text/plain":
        return ParseResult(_clean(_bounded_text(content)))
    if content_type == "text/html":
        parser = _TextExtractor()
        parser.feed(_bounded_text(content))
        parser.close()
        return ParseResult(_clean("".join(parser.parts)))
    if content_type == "application/rss+xml":
        raw = _bounded_text(content)
        try:
            root = SafeET.fromstring(raw)
        except Exception as exc:
            raise ValueError("invalid or unsafe RSS/XML") from exc
        items: list[ParsedItem] = []
        for node in root.iter():
            if node.tag.rsplit("}", 1)[-1].lower() not in {"item", "entry"}:
                continue
            fields: dict[str, str] = {}
            for child in node:
                key = child.tag.rsplit("}", 1)[-1].lower()
                if key in {"title", "description", "summary", "content", "pubdate", "published", "updated", "link", "guid", "id"}:
                    fields[key] = child.attrib.get("href", "") if key == "link" else " ".join(child.itertext()).strip()
            body = fields.get("description") or fields.get("summary") or fields.get("content") or ""
            items.append(ParsedItem(fields.get("title", ""), _clean(body), fields.get("pubdate") or fields.get("published") or fields.get("updated"), fields.get("link") or fields.get("guid") or fields.get("id")))
            if len(items) >= 500:
                return ParseResult("", tuple(items), ("RSS item limit reached (500)",))
        return ParseResult("", tuple(items))
    if content_type == "application/pdf":
        try:
            payload = base64.b64decode(_bounded_text(content), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("PDF content must be valid base64") from exc
        if len(payload) > MAX_PDF_BYTES:
            raise ValueError(f"PDF exceeds {MAX_PDF_BYTES} bytes")
        try:
            from io import BytesIO
            reader = PdfReader(BytesIO(payload), strict=True)
            if reader.is_encrypted:
                raise ValueError("encrypted PDFs are not supported")
            page_count = len(reader.pages)
            warnings = []
            if page_count > MAX_PDF_PAGES:
                warnings.append(f"PDF page limit reached ({MAX_PDF_PAGES} of {page_count})")
            extracted = []
            has_text = False
            for index, page in enumerate(reader.pages[:MAX_PDF_PAGES], start=1):
                page_text = page.extract_text() or ""
                has_text = has_text or bool(page_text.strip())
                extracted.append(f"[page {index}]\n{page_text}")
            result = _clean("\n".join(extracted)) if has_text else ""
            if len(result) > MAX_TEXT_CHARS:
                result = result[:MAX_TEXT_CHARS]
                warnings.append(f"PDF text limit reached ({MAX_TEXT_CHARS} characters)")
            if not has_text:
                warnings.append("PDF contains no extractable text; it may be scanned")
            return ParseResult(result, warnings=tuple(warnings))
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("invalid PDF") from exc
    raise ValueError(f"unsupported content_type: {content_type}")
