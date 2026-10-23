from __future__ import annotations

import base64
import unittest
from io import BytesIO

from pypdf import PdfWriter

from intelligence.parsers import parse_content


class ParserTests(unittest.TestCase):
    def test_html_strips_scripts_and_extracts_text(self):
        result = parse_content("<h1>Заголовок</h1><script>ignore()</script><p>Текст</p>", "text/html")
        self.assertIn("Заголовок", result.text)
        self.assertIn("Текст", result.text)
        self.assertNotIn("ignore", result.text)

    def test_rss_extracts_items(self):
        xml = "<rss><channel><item><title>Supply</title><description>Цепочка поставок</description><pubDate>Mon</pubDate></item></channel></rss>"
        result = parse_content(xml, "application/rss+xml")
        self.assertEqual("Supply", result.items[0].title)
        self.assertEqual("Цепочка поставок", result.items[0].text)
        self.assertEqual("Mon", result.items[0].published_at)

    def test_rss_rejects_entity_expansion(self):
        xml = '<!DOCTYPE x [<!ENTITY secret "should-not-expand">]><rss><channel><item><title>&secret;</title></item></channel></rss>'
        with self.assertRaisesRegex(ValueError, "unsafe RSS"):
            parse_content(xml, "application/rss+xml")

    def test_invalid_pdf_is_reported(self):
        with self.assertRaisesRegex(ValueError, "invalid PDF"):
            parse_content(base64.b64encode(b"not a pdf").decode(), "application/pdf")

    def test_textless_pdf_warns_scanned(self):
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        output = BytesIO()
        writer.write(output)
        result = parse_content(base64.b64encode(output.getvalue()).decode(), "application/pdf")
        self.assertTrue(any("scanned" in warning for warning in result.warnings))
