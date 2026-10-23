from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from intelligence.corpus import CorpusStore


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
