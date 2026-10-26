from __future__ import annotations

import tempfile
import threading
import unittest
import base64
import hashlib
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pypdf import PdfWriter

from intelligence.corpus import CorpusStore
from intelligence.parsers import MAX_PDF_BYTES
from intelligence.sqlite_import import import_database


def _legacy_corpus(path: Path) -> None:
    import sqlite3
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE documents (document_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                source_id TEXT NOT NULL, source_role TEXT NOT NULL, canonical_uri TEXT NOT NULL,
                title TEXT NOT NULL, publisher TEXT NOT NULL, language TEXT NOT NULL,
                synthetic INTEGER NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(tenant_id,source_id,canonical_uri));
            CREATE TABLE revisions (document_revision_ref TEXT PRIMARY KEY,
                document_id TEXT NOT NULL REFERENCES documents(document_id),
                revision INTEGER NOT NULL, content_hash TEXT NOT NULL, title TEXT NOT NULL,
                published_at TEXT NOT NULL, fetched_at TEXT NOT NULL, content_type TEXT NOT NULL,
                extracted_text TEXT NOT NULL, warnings TEXT NOT NULL, UNIQUE(document_id,revision));
            INSERT INTO documents VALUES ('doc_old','t1','news','external',
                'https://example.test/old','Old','Old publisher','ru',0,'2026-01-01T00:00:00Z');
            INSERT INTO revisions VALUES ('doc_old:1','doc_old',1,'hash','Old',
                '2026-01-01T00:00:00Z','2026-01-02T00:00:00Z','text/plain','Old body','[]');
        """)


def _import_legacy_corpus(source: Path, logical_path: Path) -> None:
    import_database(source, "corpus", logical_path, confirm_quiesced=True)


class CorpusStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CorpusStore(Path(self.tmp.name) / "corpus.sqlite")

    def tearDown(self):
        self.tmp.cleanup()

    def doc(self, *, tenant="t1", source="news", uri="https://example.test/a", title="A", text="Российский рынок меняется. Supply chain disruption.", published="2026-01-01T00:00:00+00:00", fetched="2026-01-02T00:00:00+00:00", role="external"):
        return {"tenant_id": tenant, "source_id": source, "source_role": role, "canonical_uri": uri, "title": title, "content": text, "content_type": "text/plain", "published_at": published, "fetched_at": fetched, "synthetic": False}

    def test_tenant_source_role_and_as_of_are_applied_before_ranking(self):
        self.store.ingest(self.doc())
        other = self.store.ingest(self.doc(tenant="other", uri="https://example.test/b", text="Российский рынок меняется. Supply chain disruption."))
        self.assertIsNone(other["duplicate_of"])
        self.store.ingest(self.doc(source="unapproved", uri="https://example.test/c", text="Рынок рынок рынок рынок рынок"))
        self.store.ingest(self.doc(role="bank", uri="https://example.test/d", text="Рынок рынок рынок рынок рынок"))
        self.store.ingest(self.doc(uri="https://example.test/future", fetched="2027-01-01T00:00:00+00:00", text="Рынок рынок рынок рынок рынок"))
        results = self.store.search(tenant_id="t1", source_ids=["news"], role="external", as_of="2026-06-01T00:00:00Z", query="рынок disruption", limit=20)
        self.assertEqual(1, len(results))
        self.assertEqual("A", results[0]["title"])
        self.assertTrue(results[0]["locator"].startswith("chars:0-"))
        self.assertIsNone(self.store.get_evidence(tenant_id="other", source_ids=["news"], as_of="2026-06-01T00:00:00Z", evidence_id=results[0]["evidence_id"]))

    def test_revisions_are_immutable_and_old_hidden_until_explicitly_requested(self):
        first = self.store.ingest(self.doc(text="Первоначальный текст о поставках."))
        second = self.store.ingest(self.doc(text="Новая версия о поставках и экспорте.", fetched="2026-02-01T00:00:00+00:00"))
        self.assertEqual(1, first["revision"])
        self.assertEqual(2, second["revision"])
        standard = self.store.search(tenant_id="t1", source_ids=["news"], as_of="2026-03-01T00:00:00Z", query="поставках", limit=10)
        self.assertEqual([second["document_revision_ref"]], [r["document_revision_ref"] for r in standard])
        old = self.store.search(tenant_id="t1", source_ids=["news"], as_of="2026-03-01T00:00:00Z", query="поставках", document_revision_refs=[first["document_revision_ref"]], limit=10)
        self.assertEqual([first["document_revision_ref"]], [r["document_revision_ref"] for r in old])
        old_evidence = self.store.search(tenant_id="t1", source_ids=["news"], as_of="2026-01-15T00:00:00Z", query="поставках")[0]
        self.assertIsNone(self.store.get_evidence(tenant_id="t1", source_ids=["news"], as_of="2026-03-01T00:00:00Z", evidence_id=old_evidence["evidence_id"]))

    def test_private_case_pdf_file_import_preserves_large_original_hash_and_scope(self):
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        output = BytesIO()
        writer.write(output)
        prefix = output.getvalue()
        payload = prefix + b" " * (MAX_PDF_BYTES + 1 - len(prefix))
        self.assertTrue(payload.startswith(b"%PDF-"))
        case_root = Path(self.tmp.name) / "case"
        case_root.mkdir()
        pdf_path = case_root / "bank.pdf"
        pdf_path.write_bytes(payload)
        document = self.doc(source="case-bank-1", uri="https://case-data.invalid/bank.pdf", role="bank")
        document.update(content="", content_type="application/pdf", data_classification="confidential")

        ocr_text = "[page 1]\nПоставки стали сократились; OCR требует проверки по оригинальной странице."
        imported = self.store.ingest_private_case_pdf(document, pdf_path, allowed_root=case_root, ocr_text=ocr_text)

        self.assertTrue(imported["document_revision_ref"].endswith(":1"))
        self.assertEqual(hashlib.sha256(payload).hexdigest(), imported["raw_sha256"])
        self.assertTrue(Path(imported["raw_ref"]).is_file())
        self.assertEqual(payload, Path(imported["raw_ref"]).read_bytes())
        repeated = self.store.ingest_private_case_pdf(document, pdf_path, allowed_root=case_root, ocr_text=ocr_text)
        self.assertEqual(imported["document_revision_ref"], repeated["document_revision_ref"])
        changed_bytes = prefix + b"\t" * (MAX_PDF_BYTES + 1 - len(prefix))
        pdf_path.write_bytes(changed_bytes)
        changed = self.store.ingest_private_case_pdf(document, pdf_path, allowed_root=case_root, ocr_text=ocr_text)
        self.assertEqual(2, changed["revision"])
        self.assertNotEqual(imported["raw_sha256"], changed["raw_sha256"])
        evidence = self.store.search(tenant_id="t1", source_ids=["case-bank-1"], role="bank",
                                     as_of="2026-02-01T00:00:00Z", query="стали сократились", limit=3)
        self.assertEqual(changed["document_revision_ref"], evidence[0]["document_revision_ref"])
        self.assertTrue(evidence[0]["locator"].startswith("page:1;"))
        api_document = {**document, "content": base64.b64encode(payload).decode("ascii")}
        with self.assertRaisesRegex(ValueError, "base64 exceeds|exceeds .* bytes"):
            self.store.ingest({**api_document, "source_id": "case-bank-api", "canonical_uri": "https://case-data.invalid/api.pdf"})
        outside = Path(self.tmp.name) / "outside.pdf"
        outside.write_bytes(payload[:len(prefix)])
        with self.assertRaisesRegex(ValueError, "outside_allowed_root"):
            self.store.ingest_private_case_pdf(document, outside, allowed_root=case_root)

    def test_exact_duplicates_are_linked_and_reprints_do_not_fill_ranking(self):
        first = self.store.ingest(self.doc())
        same_uri = self.store.ingest(self.doc())
        reprint = self.store.ingest(self.doc(uri="https://example.test/reprint", title="Reprint"))
        self.assertEqual(1, same_uri["revision"])
        self.assertEqual(first["document_id"], reprint["duplicate_of"])
        results = self.store.search(tenant_id="t1", source_ids=["news"], as_of="2026-06-01T00:00:00Z", query="рынок supply", limit=10)
        self.assertEqual(1, len(results))
        self.assertEqual(first["content_hash"], results[0]["content_hash"])
        evidence_id = results[0]["evidence_id"]
        self.assertEqual(__import__("hashlib").sha256(results[0]["quote"].encode()).hexdigest(), results[0]["content_hash"])
        again = self.store.search(tenant_id="t1", source_ids=["news"], as_of="2026-06-01T00:00:00Z", query="рынок supply")[0]
        self.assertEqual(evidence_id, again["evidence_id"])

    def test_filters_run_before_limit_even_with_high_scoring_forbidden_rows(self):
        self.store.ingest(self.doc(tenant="private", uri="x1", text="газ газ газ экспорт экспорт"))
        self.store.ingest(self.doc(uri="x2", text="экспорт и один источник"))
        rows = self.store.search(tenant_id="t1", source_ids=["news"], as_of="2026-06-01T00:00:00Z", query="газ экспорт", limit=1)
        self.assertEqual(1, len(rows))
        self.assertEqual("x2", rows[0]["canonical_uri"])

    def test_rss_entries_are_ingested_as_separate_documents_with_their_own_dates(self):
        feed = self.doc(uri="https://example.test/feed", title="Feed", text="", published="2026-01-01T00:00:00Z")
        feed["content_type"] = "application/rss+xml"
        feed["content"] = "<rss><channel><item><title>Old</title><link>https://example.test/old</link><pubDate>Mon, 01 Dec 2025 00:00:00 GMT</pubDate><description>Рынок старый</description></item><item><title>New</title><link>https://example.test/new</link><pubDate>Tue, 02 Feb 2026 00:00:00 GMT</pubDate><description>Рынок новый</description></item></channel></rss>"
        outcome = self.store.ingest(feed)
        self.assertEqual(2, len(outcome["items"]))
        rows = self.store.search(tenant_id="t1", source_ids=["news"], as_of="2026-01-15T00:00:00Z", query="рынок", limit=10)
        self.assertEqual(["Old"], [r["title"] for r in rows])
        self.assertEqual("2025-12-01T00:00:00.000000Z", rows[0]["published_at"])

    def test_rss_text_link_keeps_document_identity_across_corrected_content(self):
        feed = self.doc(uri="https://example.test/feed", title="Feed", text="", published="2026-01-01T00:00:00Z")
        feed["content_type"] = "application/rss+xml"
        feed["content"] = "<rss><channel><item><title>First</title><link>https://example.test/article</link><description>Текст первоначальный</description></item></channel></rss>"
        first = self.store.ingest(feed)["items"][0]
        feed["content"] = "<rss><channel><item><title>Corrected</title><link>https://example.test/article</link><description>Исправленный текст</description></item></channel></rss>"
        second = self.store.ingest({**feed, "fetched_at": "2026-02-01T00:00:00Z"})["items"][0]
        self.assertEqual(first["document_id"], second["document_id"])
        self.assertEqual([1, 2], [first["revision"], second["revision"]])

    def test_metadata_correction_is_versioned_and_same_observation_is_immutable(self):
        first_doc = self.doc(text="Стабильный материал о поставках.")
        first_doc.update(title="Первый заголовок", publisher="Первое издание", language="ru")
        first = self.store.ingest(first_doc)
        corrected = {**first_doc, "title": "Исправленный заголовок", "published_at": "2026-01-01T00:00:00Z", "publisher": "Второе издание", "language": "en", "fetched_at": "2026-02-01T00:00:00Z"}
        second = self.store.ingest(corrected)
        self.assertEqual(2, second["revision"])
        old = self.store.search(tenant_id="t1", source_ids=["news"], as_of="2026-01-15T00:00:00Z", query="поставках")[0]
        new = self.store.search(tenant_id="t1", source_ids=["news"], as_of="2026-03-01T00:00:00Z", query="поставках")[0]
        self.assertEqual("Первый заголовок", old["title"])
        self.assertEqual("Исправленный заголовок", new["title"])
        def metadata_as_of(as_of):
            with self.store._connect() as db:
                return db.execute("""SELECT r.publisher,r.language FROM revisions r
                    WHERE r.document_id=%s AND r.fetched_at<=%s AND r.published_at<=%s
                    ORDER BY r.revision DESC LIMIT 1""", (first["document_id"], as_of, as_of)).fetchone()
        old_metadata = metadata_as_of("2026-01-15T00:00:00.000000Z")
        new_metadata = metadata_as_of("2026-03-01T00:00:00.000000Z")
        self.assertEqual(("Первое издание", "ru"), (old_metadata["publisher"], old_metadata["language"]))
        self.assertEqual(("Второе издание", "en"), (new_metadata["publisher"], new_metadata["language"]))
        repeated = self.store.ingest({**corrected, "fetched_at": "2026-02-05T00:00:00Z"})
        self.assertEqual(2, repeated["revision"])
        same = self.store.search(tenant_id="t1", source_ids=["news"], as_of="2026-03-01T00:00:00Z", query="поставках")[0]
        self.assertEqual("2026-02-01T00:00:00.000000Z", same["fetched_at"])
        backdated = self.store.ingest({**corrected, "title": "Backdated", "fetched_at": "2026-01-10T00:00:00Z"})
        self.assertEqual(3, backdated["revision"])
        backdated_result = self.store.search(tenant_id="t1", source_ids=["news"], as_of="2026-03-01T00:00:00Z", query="поставках")[0]
        self.assertEqual("2026-02-01T00:00:00.000001Z", backdated_result["fetched_at"])
        at_prior_known_at = self.store.search(tenant_id="t1", source_ids=["news"], as_of="2026-02-01T00:00:00Z", query="поставках")[0]
        self.assertEqual("Исправленный заголовок", at_prior_known_at["title"])

    def test_legacy_revision_schema_is_migrated_additively(self):
        source = Path(self.tmp.name) / "legacy.sqlite"
        logical_path = Path(self.tmp.name) / "legacy-corpus-store"
        _legacy_corpus(source)
        _import_legacy_corpus(source, logical_path)
        migrated = CorpusStore(logical_path)
        with migrated._connect() as db:
            row = db.execute("SELECT publisher,language FROM revisions WHERE document_revision_ref='doc_old:1'").fetchone()
        self.assertEqual(("Old publisher", "ru"), (row["publisher"], row["language"]))

    def test_concurrent_stores_read_legacy_schema_after_import(self):
        source = Path(self.tmp.name) / "concurrent-legacy.sqlite"
        logical_path = Path(self.tmp.name) / "concurrent-corpus-store"
        _legacy_corpus(source)
        _import_legacy_corpus(source, logical_path)
        barrier = threading.Barrier(2)
        def initialize(_):
            barrier.wait()
            return CorpusStore(logical_path)
        with ThreadPoolExecutor(max_workers=2) as pool:
            stores = list(pool.map(initialize, range(2)))
        with stores[0]._connect() as db:
            columns = {row["column_name"] for row in db.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema='ai_corpus' AND table_name='revisions'")}
            row = db.execute("SELECT publisher,language FROM revisions WHERE document_revision_ref='doc_old:1'").fetchone()
        self.assertTrue({"publisher", "language"}.issubset(columns))
        self.assertEqual(("Old publisher", "ru"), (row["publisher"], row["language"]))

    def test_canonical_uri_source_role_and_synthetic_are_immutable(self):
        self.store.ingest(self.doc())
        with self.assertRaisesRegex(ValueError, "immutable"):
            self.store.ingest(self.doc(role="bank", fetched="2026-02-01T00:00:00Z"))

    def test_concurrent_ingests_allocate_unique_revisions(self):
        def add(i):
            return self.store.ingest(self.doc(text=f"Ревизия {i} и поставки", fetched=f"2026-01-0{i + 1}T00:00:00Z"))["revision"]
        with ThreadPoolExecutor(max_workers=5) as pool:
            revisions = list(pool.map(add, range(5)))
        self.assertEqual([1, 2, 3, 4, 5], sorted(revisions))

    def test_rejects_naive_datetimes(self):
        with self.assertRaisesRegex(ValueError, "timezone"):
            self.store.ingest(self.doc(fetched="2026-01-01T00:00:00"))
