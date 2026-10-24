import hashlib
import json
import sqlite3
import socket
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from intelligence.collection import (
    FetchResult,
    JSONAdapterMapping,
    NewsCollector,
    SourceGrant,
    SourcePolicySnapshot,
    SourceProbeRequest,
    _public_addresses,
)
from intelligence.corpus import CorpusStore


NOW = datetime(2026, 10, 24, tzinfo=timezone.utc)
RSS = '''<?xml version="1.0"?><rss version="2.0"><channel xml:lang="ru"><title>Проверенное издание</title><item><title>Событие</title><description>Рынок и поставки.</description><pubDate>Fri, 23 Oct 2026 10:00:00 +0000</pubDate><event_time>2026-10-20T00:00:00Z</event_time><dc:creator xmlns:dc="http://purl.org/dc/elements/1.1/">Автор публикации</dc:creator><link>https://news.example/story</link></item><item><title>Без даты</title><description>Не сохранять.</description><link>https://news.example/undated</link></item></channel></rss>'''.encode()


class FakeFetcher:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers, **options):
        self.calls.append((url, dict(headers), options))
        return self.responses.pop(0)


def source(**kwargs):
    return {
        "source_id": "news",
        "source_role": "external",
        "uri": "https://feed.example/rss",
        "content_type": "application/rss+xml",
        "publisher": "",
        "article_hosts": ("news.example",),
        **kwargs,
    }


def snapshot(*sources, **kwargs):
    tenant_id = kwargs.pop("tenant_id", "tenant-a")
    return SourcePolicySnapshot(policy_id="policy-1", revision=3, tenant_id=tenant_id, issued_at=NOW, sources=tuple(sources), **kwargs)


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = CorpusStore(self.root / "corpus.sqlite")
        self.raw = self.root / "raw"
        self.collector_state = self.root / "poll.sqlite"
        self.grant = source()

    def tearDown(self):
        self.temp.cleanup()

    def test_rss_collection_preserves_dates_original_bytes_and_provenance(self):
        fetcher = FakeFetcher(FetchResult(status=200, headers={"content-type": "application/rss+xml", "etag": '"a"'}, body=RSS))
        collector = NewsCollector(self.store, state_path=self.collector_state, fetcher=fetcher, raw_dir=self.raw)
        report = collector.collect(snapshot(self.grant), now=NOW)
        self.assertEqual(report["fetched"], 1)
        self.assertEqual(report["policy_revision"], 3)
        self.assertEqual(len(report["documents"]), 1)
        document = report["documents"][0]
        self.assertEqual(document["canonical_uri"], "https://news.example/story")
        self.assertEqual(document["published_at"], "2026-10-23T10:00:00.000000Z")
        self.assertEqual(document["publisher"], "Проверенное издание")
        self.assertEqual(document["author"], "Автор публикации")
        self.assertEqual(document["event_time"], "2026-10-20T00:00:00.000000Z")
        self.assertEqual(document["language"], "ru")
        self.assertEqual(document["data_classification"], "confidential")
        self.assertFalse(document["synthetic"])
        self.assertEqual(document["raw_sha256"], hashlib.sha256(RSS).hexdigest())
        self.assertEqual(Path(document["raw_ref"]).read_bytes(), RSS)
        self.assertNotIn("tenant-a", document["raw_ref"])
        corpus_doc = self.store.list_documents(tenant_id="tenant-a", source_ids=["news"], as_of=NOW.isoformat())[0]
        self.assertEqual(corpus_doc["author"], "Автор публикации")
        self.assertEqual(corpus_doc["event_time"], "2026-10-20T00:00:00.000000Z")
        self.assertEqual(corpus_doc["data_classification"], "confidential")
        self.assertTrue(document["document_revision_ref"])
        self.assertEqual(report["pending_metadata"][0]["missing_fields"], ["published_at"])

    def test_source_classification_is_authoritative_and_unknown_defaults_confidential(self):
        fetcher = FakeFetcher(FetchResult(status=200,
            headers={"content-type": "application/rss+xml"}, body=RSS))
        collector = NewsCollector(self.store, state_path=self.collector_state, fetcher=fetcher)
        public_report = collector.collect(snapshot(source(data_classification="public")), now=NOW)
        self.assertEqual(public_report["documents"][0]["data_classification"], "public")
        self.assertFalse(public_report["documents"][0]["synthetic"])
        public_doc = self.store.list_documents(tenant_id="tenant-a", source_ids=["news"],
            as_of=NOW.isoformat())[0]
        self.assertEqual(public_doc["data_classification"], "public")

        synthetic_feed = RSS.replace("Событие".encode(), "Демо-событие".encode())
        synthetic_fetcher = FakeFetcher(FetchResult(status=200,
            headers={"content-type": "application/rss+xml"}, body=synthetic_feed))
        synthetic_collector = NewsCollector(self.store, state_path=self.root / "synthetic-poll.sqlite",
            fetcher=synthetic_fetcher)
        synthetic_report = synthetic_collector.collect(
            snapshot(source(source_id="synthetic", data_classification="synthetic")), now=NOW)
        self.assertEqual(synthetic_report["documents"][0]["data_classification"], "synthetic")
        self.assertTrue(synthetic_report["documents"][0]["synthetic"])
        synthetic_doc = self.store.list_documents(tenant_id="tenant-a", source_ids=["synthetic"],
            as_of=NOW.isoformat())[0]
        self.assertEqual(synthetic_doc["data_classification"], "synthetic")

    def test_policy_exclusions_cover_source_publisher_and_specific_document(self):
        first = {**self.grant, "source_id": "first", "publisher": "Press"}
        second = {**self.grant, "source_id": "second", "uri": "https://other.example/feed", "publisher": "Other"}
        policy = snapshot(first, second, excluded_publishers=("press",), user_excluded_document_uris=("https://other.example/story",))
        self.assertEqual(policy.effective_source_ids(), ["second"])
        self.assertFalse(policy.permits_document({"tenant_id": "tenant-a", "source_id": "first", "publisher": "Press", "canonical_uri": "https://press.example/a"}))
        personal_hidden = {"tenant_id": "tenant-a", "source_id": "second", "publisher": "Other", "canonical_uri": "https://other.example/story"}
        self.assertTrue(policy.permits_document(personal_hidden))  # personal hide is not company-analysis exclusion
        self.assertTrue(policy.is_personally_hidden(personal_hidden))
        self.assertEqual(policy.effective_source_ids(), ["second"])
        company_excluded = snapshot(second, excluded_document_uris=("https://other.example/story",))
        self.assertFalse(company_excluded.permits_document(personal_hidden))
        self.assertFalse(policy.permits_document({"tenant_id": "tenant-b", "source_id": "second", "publisher": "Other", "canonical_uri": "https://other.example/ok"}))
        self.assertTrue(policy.permits_document({"tenant_id": "tenant-a", "source_id": "second", "publisher": "Other", "canonical_uri": "https://other.example/ok"}))
        self.assertFalse(policy.permits_document({"tenant_id": "tenant-a", "source_id": "second", "source_role": "bank", "publisher": "Other", "canonical_uri": "https://other.example/ok"}))

    def test_html_document_exclusion_is_checked_after_metadata_parse_before_raw_write(self):
        html = b'<html><head><title>Notice</title><meta property="og:site_name" content="Press"><meta property="article:published_time" content="2026-10-23T10:00:00Z"></head><body><article>Full text</article></body></html>'
        grant = source(uri="https://news.example/article", content_type="text/html")
        fetcher = FakeFetcher(FetchResult(status=200, headers={"content-type": "text/html"}, body=html))
        collector = NewsCollector(self.store, state_path=self.collector_state, fetcher=fetcher, raw_dir=self.raw)
        report = collector.collect(snapshot(grant, excluded_publishers=("press",)), now=NOW)
        self.assertEqual(report["fetched"], 1)
        self.assertEqual(report["documents"], [])
        self.assertEqual(list(self.raw.rglob("*")), [])

    def test_known_html_uri_exclusion_is_checked_before_network(self):
        grant = source(uri="https://news.example/article", content_type="text/html")
        fetcher = FakeFetcher()
        collector = NewsCollector(self.store, state_path=self.collector_state, fetcher=fetcher, raw_dir=self.raw)
        report = collector.collect(snapshot(grant, excluded_document_uris=(grant["uri"],)), now=NOW)
        self.assertEqual(report["skipped"][0]["reason"], "policy_excluded_document")
        self.assertEqual(fetcher.calls, [])

    def test_excluded_source_is_never_requested_and_source_ids_cannot_escape_snapshot(self):
        fetcher = FakeFetcher(FetchResult(status=200, headers={}, body=RSS))
        collector = NewsCollector(self.store, state_path=self.collector_state, fetcher=fetcher)
        policy = snapshot(self.grant, excluded_source_ids=("news",))
        self.assertEqual(collector.collect(policy, now=NOW)["sources_considered"], 0)
        self.assertEqual(fetcher.calls, [])
        with self.assertRaises(ValueError):
            collector.collect(snapshot(self.grant), now=NOW, source_ids=["not-in-snapshot"])

    def test_conditional_polling_and_304_respect_next_poll(self):
        fetcher = FakeFetcher(FetchResult(status=304, headers={}, body=b""))
        collector = NewsCollector(self.store, state_path=self.collector_state, fetcher=fetcher)
        collector._save_state("tenant-a", SourceGrant.model_validate(self.grant), NOW + timedelta(hours=1), {"attempts": 2, "etag": '"v1"', "last_modified": None}, None, None, success=True)
        policy = snapshot(self.grant)
        self.assertEqual(len(collector.collect(policy, now=NOW)["skipped"]), 1)
        report = collector.collect(policy, now=NOW, force=True)
        self.assertEqual(report["unchanged"], 1)
        self.assertEqual(fetcher.calls[0][1]["If-None-Match"], '"v1"')

    def test_changed_grant_resets_poll_due_and_conditional_validators(self):
        original = SourceGrant.model_validate(self.grant)
        collector = NewsCollector(self.store, state_path=self.collector_state,
            fetcher=FakeFetcher(FetchResult(status=200, headers={"etag": '"new"'}, body=RSS)))
        collector._save_state("tenant-a", original, NOW + timedelta(hours=1),
            {"attempts": 3, "etag": '"old"', "last_modified": "yesterday"}, None, None, success=True)
        changed = {**self.grant, "uri": "https://feed.example/replacement.xml"}
        fetcher = collector.fetcher
        report = collector.collect(snapshot(changed), now=NOW + timedelta(minutes=1))
        self.assertEqual(report["fetched"], 1)
        self.assertEqual(fetcher.calls[0][0], changed["uri"])
        self.assertEqual(fetcher.calls[0][1], {})
        state = collector._state("tenant-a", "news")
        self.assertEqual(state["etag"], '"new"')
        self.assertEqual(state["grant_fingerprint"], collector._grant_fingerprint(SourceGrant.model_validate(changed)))

    def test_old_grant_response_cannot_overwrite_new_grant_poll_state(self):
        collector = NewsCollector(self.store, state_path=self.collector_state, fetcher=FakeFetcher())
        old = SourceGrant.model_validate(self.grant)
        new = SourceGrant.model_validate({**self.grant, "uri": "https://feed.example/current.xml"})
        collector._save_state("tenant-a", new, NOW + timedelta(hours=1),
            {"attempts": 0, "etag": '"current"', "last_modified": None}, None, None, success=True)
        collector._save_state("tenant-a", old, NOW + timedelta(minutes=5),
            {"attempts": 1, "etag": '"stale"', "last_modified": None},
            FetchResult(status=200, headers={"etag": '"stale"'}, body=b"old"), None, success=True)
        state = collector._state("tenant-a", "news")
        self.assertEqual(state["etag"], '"current"')
        self.assertEqual(state["grant_fingerprint"], collector._grant_fingerprint(new))

    def test_grant_classification_change_never_promotes_existing_corpus_revision(self):
        confidential = NewsCollector(self.store, state_path=self.collector_state,
            fetcher=FakeFetcher(FetchResult(status=200, headers={"etag": '"old"'}, body=RSS)))
        first = confidential.collect(snapshot(self.grant), now=NOW)
        self.assertEqual(first["documents"][0]["data_classification"], "confidential")
        public_grant = {**self.grant, "data_classification": "public"}
        updated_rss = RSS.replace("Рынок и поставки.".encode(), "Рынок и поставки изменились.".encode())
        public = NewsCollector(self.store, state_path=self.collector_state,
            fetcher=FakeFetcher(FetchResult(status=200, headers={"etag": '"new"'}, body=updated_rss)))
        refreshed = public.collect(snapshot(public_grant), now=NOW + timedelta(minutes=1))
        self.assertEqual(refreshed["fetched"], 1)
        self.assertNotEqual(refreshed["documents"][0]["document_revision_ref"], first["documents"][0]["document_revision_ref"])
        self.assertEqual(refreshed["documents"][0]["data_classification"], "confidential")
        self.assertEqual(Path(first["documents"][0]["raw_ref"]).read_bytes(), RSS)
        self.assertEqual(Path(refreshed["documents"][0]["raw_ref"]).read_bytes(), updated_rss)
        stored = self.store.list_documents(tenant_id="tenant-a", source_ids=["news"],
            as_of=(NOW + timedelta(minutes=1)).isoformat())
        self.assertEqual(stored[0]["data_classification"], "confidential")

    def test_poll_state_additive_migration_is_serialized_and_adds_fingerprint(self):
        legacy_path = self.root / "legacy-poll.sqlite"
        with sqlite3.connect(legacy_path) as db:
            db.execute("CREATE TABLE poll_state (tenant_id TEXT NOT NULL, source_id TEXT NOT NULL, next_poll TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, etag TEXT, last_modified TEXT, last_error TEXT, last_status INTEGER, PRIMARY KEY(tenant_id,source_id))")
        NewsCollector(self.store, state_path=legacy_path, fetcher=FakeFetcher())
        with sqlite3.connect(legacy_path) as db:
            cols = {row[1] for row in db.execute("PRAGMA table_info(poll_state)")}
        self.assertTrue({"lease_until", "grant_fingerprint"}.issubset(cols))

    def test_html_probe_detects_metadata_but_does_not_invent_missing_date(self):
        html = '<html><head><title>Заголовок</title><meta property="og:site_name" content="Издание"></head><body><article><p>Текст статьи.</p></article></body></html>'.encode()
        fetcher = FakeFetcher(FetchResult(status=200, headers={"content-type": "text/html; charset=utf-8"}, body=html))
        collector = NewsCollector(self.store, state_path=self.collector_state, fetcher=fetcher)
        request = SourceProbeRequest(grant_id="probe-1", tenant_id="tenant-a", candidate_url="https://news.example/article", expires_at=NOW + timedelta(minutes=1), allowed_hosts=("news.example",), allowed_types=("text/html",))
        result = collector.probe(request, now=NOW)
        self.assertEqual(result.status, "needs_metadata")
        self.assertEqual(result.title, "Заголовок")
        self.assertEqual(result.publisher, "Издание")
        self.assertIsNone(result.published_at)
        self.assertIn("published_at", result.missing_fields)
        self.assertEqual(result.raw_sha256, hashlib.sha256(html).hexdigest())

    def test_probe_grant_expiry_and_host_are_enforced(self):
        collector = NewsCollector(self.store, state_path=self.collector_state, fetcher=FakeFetcher())
        expired = SourceProbeRequest(grant_id="g", tenant_id="tenant-a", candidate_url="https://news.example/", expires_at=NOW - timedelta(seconds=1), allowed_hosts=("news.example",), allowed_types=("text/html",))
        self.assertEqual(collector.probe(expired, now=NOW).error_code, "probe_grant_expired")
        wrong_host = SourceProbeRequest(grant_id="g", tenant_id="tenant-a", candidate_url="https://news.example/", expires_at=NOW + timedelta(minutes=1), allowed_hosts=("elsewhere.example",), allowed_types=("text/html",))
        self.assertEqual(collector.probe(wrong_host, now=NOW).error_code, "probe_host_not_allowed")

    def test_redirects_are_followed_only_within_policy_hosts(self):
        fetcher = FakeFetcher(FetchResult(status=302, headers={"location": "https://outside.example/feed"}, body=b""))
        collector = NewsCollector(self.store, state_path=self.collector_state, fetcher=fetcher)
        result = collector.collect(snapshot(self.grant), now=NOW)
        self.assertEqual(result["errors"][0]["error"], "redirect_host_not_allowed")
        self.assertEqual(len(fetcher.calls), 1)

    def test_json_adapter_is_static_bounded_dot_path_mapping(self):
        mapping = JSONAdapterMapping(items="data.items", title="title", content="text", url="link", published_at="published", author="author")
        api_source = {"source_id": "api", "source_role": "external", "uri": "https://api.example/articles", "content_type": "application/json", "article_hosts": ("api.example",), "json_mapping": mapping}
        body = json.dumps({"data": {"items": [{"title": "Наблюдение", "text": "Рынок меняется.", "link": "https://api.example/1", "published": "2026-10-23T12:00:00Z", "author": "Редакция"}, {"title": "Без даты", "text": "Текст", "link": "https://api.example/2", "published": ""}]}}).encode()
        fetcher = FakeFetcher(FetchResult(status=200, headers={"content-type": "application/json"}, body=body))
        collector = NewsCollector(self.store, state_path=self.collector_state, fetcher=fetcher)
        result = collector.collect(snapshot(api_source), now=NOW)
        self.assertEqual(len(result["documents"]), 1)
        self.assertEqual(result["documents"][0]["canonical_uri"], "https://api.example/1")
        self.assertEqual(result["documents"][0]["author"], "Редакция")
        self.assertIn("published_at", result["pending_metadata"][0]["missing_fields"])

    def test_private_and_mixed_public_private_dns_answers_are_rejected(self):
        with self.assertRaises(ValueError):
            _public_addresses("127.0.0.1")
        with patch("intelligence.collection.socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)), (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.2", 443))]):
            with self.assertRaises(ValueError):
                _public_addresses("rebind.example")
        with patch("intelligence.collection.socket.getaddrinfo", return_value=[(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::ffff:127.0.0.1", 443, 0, 0))]):
            with self.assertRaises(ValueError):
                _public_addresses("mapped.example")

    def test_sensitive_url_query_names_are_rejected(self):
        with self.assertRaises(ValueError):
            SourcePolicySnapshot.model_validate({"policy_id": "p", "revision": 1, "tenant_id": "t", "issued_at": NOW, "sources": [source(uri="https://feed.example/rss?X-Amz-Signature=hidden")]})

    def test_raw_storage_is_tenant_scoped_and_metadata_reaches_corpus(self):
        first = NewsCollector(self.store, state_path=self.collector_state, fetcher=FakeFetcher(FetchResult(status=200, headers={}, body=RSS)), raw_dir=self.raw)
        first_report = first.collect(snapshot(self.grant), now=NOW)
        first_raw = Path(first_report["documents"][0]["raw_ref"])
        second = NewsCollector(self.store, state_path=self.root / "poll-second.sqlite", fetcher=FakeFetcher(FetchResult(status=200, headers={}, body=RSS)), raw_dir=self.raw)
        second_report = second.collect(snapshot(self.grant, tenant_id="tenant-b"), now=NOW)
        second_raw = Path(second_report["documents"][0]["raw_ref"])
        self.assertEqual(first_raw.read_bytes(), second_raw.read_bytes())
        self.assertNotEqual(first_raw, second_raw)
        self.assertNotIn("tenant-a", str(first_raw))
        self.assertNotIn("tenant-b", str(second_raw))
        self.assertNotEqual(first_raw.parent.parent, second_raw.parent.parent)

    def test_concurrent_force_poll_is_singleflight_across_collector_instances(self):
        entered = threading.Event()
        release = threading.Event()

        class BlockingFetcher(FakeFetcher):
            def __call__(self, url, headers, **options):
                self.calls.append((url, dict(headers), options))
                entered.set()
                release.wait(2)
                return FetchResult(status=304, headers={}, body=b"")

        fetcher = BlockingFetcher()
        one = NewsCollector(self.store, state_path=self.collector_state, fetcher=fetcher)
        two = NewsCollector(self.store, state_path=self.collector_state, fetcher=fetcher)
        output = []
        thread = threading.Thread(target=lambda: output.append(one.collect(snapshot(self.grant), now=NOW, force=True)))
        thread.start()
        self.assertTrue(entered.wait(1))
        concurrent = two.collect(snapshot(self.grant), now=NOW, force=True)
        self.assertEqual(concurrent["skipped"][0]["reason"], "already_running")
        self.assertEqual(len(fetcher.calls), 1)
        release.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(output[0]["unchanged"], 1)


if __name__ == "__main__":
    unittest.main()
