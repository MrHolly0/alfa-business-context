from __future__ import annotations

import base64
import unittest
from io import BytesIO

from pypdf import PdfWriter

from intelligence.parsers import MAX_PDF_BASE64_CHARS, MAX_PDF_BYTES, parse_content


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

    def test_rss_text_link_and_atom_canonical_link_preserve_identity(self):
        rss = '<rss><channel><item><title>A</title><link>https://example.test/a</link><description>Body</description></item></channel></rss>'
        atom = '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>A</title><link rel="alternate" href="https://example.test/alt"/><link rel="canonical" href="https://example.test/canonical"/><summary>Body</summary></entry></feed>'
        self.assertEqual("https://example.test/a", parse_content(rss, "application/rss+xml").items[0].uri)
        self.assertEqual("https://example.test/canonical", parse_content(atom, "application/rss+xml").items[0].uri)

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

    def _pdf_of_size(self, byte_count):
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        output = BytesIO()
        writer.write(output)
        payload = output.getvalue()
        self.assertLess(len(payload), byte_count)
        return payload + b" " * (byte_count - len(payload))

    def test_pdf_base64_limit_is_independent_of_text_limit(self):
        payload = self._pdf_of_size(1_600_452)
        parsed = parse_content(base64.b64encode(payload).decode(), "application/pdf")
        self.assertTrue(any("scanned" in warning for warning in parsed.warnings))

    def test_pdf_20mb_boundary_and_overflow(self):
        exact = self._pdf_of_size(MAX_PDF_BYTES)
        encoded = base64.b64encode(exact).decode()
        self.assertEqual(MAX_PDF_BASE64_CHARS, len(encoded))
        parse_content(encoded, "application/pdf")
        over = base64.b64encode(self._pdf_of_size(MAX_PDF_BYTES + 1)).decode()
        with self.assertRaisesRegex(ValueError, "exceeds .* bytes"):
            parse_content(over, "application/pdf")
        with self.assertRaisesRegex(ValueError, "base64 exceeds"):
            parse_content("A" * (MAX_PDF_BASE64_CHARS + 1), "application/pdf")
