from pathlib import Path
import json
import multiprocessing
import os
import time

import pytest

from intelligence.events import EventStore, ImpactPackage, PublicationInput


def publication(tenant="tenant-a", source="src-a", uri="https://news.example/story?utm_source=x",
               content="Издатель сообщает о засухе в регионе. Производство сырья сократилось.", **extra):
    values=dict(tenant_id=tenant,source_id=source,source_role="external",canonical_uri=uri,
        title="Засуха в регионе",content=content,content_type="text/plain",
        published_at="2026-10-24T08:00:00+03:00",fetched_at="2026-10-24T08:03:00+03:00",
        synthetic=True,publisher="Издание",author="Редакция")
    values.update(extra)
    return PublicationInput(**values)


@pytest.fixture
def store(tmp_path):
    return EventStore(tmp_path / "events.sqlite3")


def policy(store, event_id, tenant="tenant-a", allowed=("src-a","src-b"), revision=None):
    return store.get_event(tenant,event_id,source_policy_id="policy-1",policy_version="v1",
                           allowed_source_ids=list(allowed),revision=revision)


def test_same_canonical_publication_is_idempotent_and_correction_versions(store):
    first=store.ingest(publication())
    repeated=store.ingest(publication(uri="https://news.example/story/"))
    assert first["event_id"]==repeated["event_id"]
    assert repeated["changed"] is False
    correction=store.ingest(publication(uri="https://news.example/story?utm_source=other",
        content="Издатель исправил сообщение: производство сырья не сократилось."))
    assert correction["event_id"]==first["event_id"]
    assert correction["revision"]==first["revision"]+1
    current=policy(store,first["event_id"])
    old=policy(store,first["event_id"],revision=first["revision"])
    assert "исправил" in current["sources"][0]["content"]
    assert "исправил" not in old["sources"][0]["content"]


def test_near_duplicate_is_grouped_but_independent_sources_remain_distinct(store):
    text="Издатель сообщает о серьёзной засухе в северном регионе. Производство основных видов сырья сократилось на двадцать процентов за последний месяц. Поставки задерживаются."
    first=store.ingest(publication(content=text))
    second=store.ingest(publication(source="src-b",uri="https://wire.example/republish",
        origin_group_id="wire-b",content=text.replace("серьёзной","сильной")))
    assert first["event_id"]==second["event_id"]
    snapshot=policy(store,first["event_id"])
    assert snapshot["coverage"]["permitted_publications"]==2
    assert snapshot["coverage"]["independent_origin_groups"]==2
    assert second["grouping"]=="near_duplicate_review_group"


def test_exact_reprints_preserve_receipts_but_do_not_count_as_independent(store):
    text="Публикация сообщает: порт закрыт на два дня из-за шторма."
    first=store.ingest(publication(uri="https://news-a.example/story",content=text))
    second=store.ingest(publication(source="src-b",uri="https://news-b.example/copy",content=text))
    snapshot=policy(store,first["event_id"])
    coverage=snapshot["coverage"]
    assert coverage["permitted_publications"]==2
    assert coverage["origin_group_count"]==2
    assert coverage["independent_origin_groups"]==1
    assert coverage["independence_status"]=="unknown"
    assert "exact_reprints_not_independent_confirmations" in coverage["limitations"]
    assert second["event_id"]==first["event_id"]


def test_tenant_and_source_policy_block_publication_and_original_leaks(store):
    first=store.ingest(publication())
    other=store.ingest(publication(tenant="tenant-b",source="src-a",content="Совершенно другое событие в другом tenant."))
    assert first["event_id"]!=other["event_id"]
    assert store.get_event("tenant-a",other["event_id"],source_policy_id="policy-1",
        policy_version="v1",allowed_source_ids=["src-a"]) is None
    assert policy(store,first["event_id"],allowed=("src-b",)) is None
    with pytest.raises(ValueError):
        store.build_impact_package("tenant-a",first["event_id"],source_policy_id="policy-1",
            policy_version="v1",allowed_source_ids=["src-b"])


def test_impact_package_is_pydantic_serializable_cached_and_honest_without_llm(store):
    first=store.ingest(publication())
    package=store.build_impact_package("tenant-a",first["event_id"],source_policy_id="policy-1",
        policy_version="v1",allowed_source_ids=["src-a"])
    decoded=ImpactPackage.model_validate(package.model_dump())
    again=store.build_impact_package("tenant-a",first["event_id"],source_policy_id="policy-1",
        policy_version="v1",allowed_source_ids=["src-a"])
    assert decoded.package_id==again.package_id
    assert all(claim.kind=="reported_fact" for claim in decoded.claims)
    assert all(claim.classification_basis.startswith("deterministic_") for claim in decoded.claims)
    assert decoded.sources[0].content_hash
    original=store.original("tenant-a",first["event_id"],decoded.sources[0].publication_id,
        source_policy_id="policy-1",policy_version="v1",allowed_source_ids=["src-a"])
    assert original["content"].startswith("Издатель сообщает")


def test_llm_claims_require_exact_quote_in_allowed_original(store):
    first=store.ingest(publication())
    class StubLLM:
        class Config: model="allowed-test-model"; max_output_tokens=512
        config=Config()
        def complete(self,*args,**kwargs):
            return {"content":'{"claims":[{"kind":"fact","statement":"Invented","quote":"not in article","source_id":"src-a","publication_id":"'+first["publication_id"]+'"},{"kind":"forecast","statement":"Публикация прогнозирует спад","quote":"Производство сырья сократилось.","source_id":"src-a","publication_id":"'+first["publication_id"]+'"}]}'}
    claims,meta=store.extract_claims("tenant-a",first["event_id"],source_policy_id="policy-1",
        policy_version="v1",allowed_source_ids=["src-a"],llm=StubLLM())
    assert len(claims)==1
    assert claims[0].kind=="forecast"
    assert meta["extraction_mode"]=="llm_with_quote_validation"


def test_same_uri_from_distinct_source_ids_creates_separate_publication_receipts(store):
    first=store.ingest(publication(source="src-a",uri="https://news.example/shared"))
    second=store.ingest(publication(source="src-b",uri="https://news.example/shared"))
    assert first["event_id"]==second["event_id"]
    assert first["publication_id"]!=second["publication_id"]
    snapshot=policy(store,first["event_id"])
    assert {source["source_id"] for source in snapshot["sources"]}=={"src-a","src-b"}


def test_package_lookup_and_semantic_claim_cache_precede_repeat_model_calls(store):
    first=store.ingest(publication(content="Публикация сообщает: порт закрыт на два дня из-за шторма."))
    class CountingLLM:
        class Config: model="claim-cache-model"; max_output_tokens=512
        config=Config()
        calls=0
        def complete(self,messages,**kwargs):
            self.calls+=1
            payload=__import__("json").loads(messages[-1]["content"].split("Данные: ",1)[1])
            source=payload[0]
            quote="Публикация сообщает: порт закрыт на два дня из-за шторма."
            return {"content":__import__("json").dumps({"claims":[{"kind":"reported_fact",
                "statement":"Источник сообщает о закрытии порта.","quote":quote,
                "source_id":source["source_id"],"publication_id":source["publication_id"]}]})}
    llm=CountingLLM()
    package=store.build_impact_package("tenant-a",first["event_id"],source_policy_id="policy-1",
        policy_version="v1",allowed_source_ids=["src-a","src-b","src-c"],llm=llm)
    assert llm.calls==1
    cached=store.build_impact_package("tenant-a",first["event_id"],source_policy_id="policy-1",
        policy_version="v1",allowed_source_ids=["src-a","src-b","src-c"],llm=llm)
    assert llm.calls==1
    assert package.package_id==cached.package_id

    # A second same-origin receipt changes event/package revision metadata but not
    # source semantics, so the expensive shared claim extraction is reused.
    store.ingest(publication(source="src-b",uri="https://mirror.example/shared",
        origin_group_id="news.example",content="Публикация сообщает: порт закрыт на два дня из-за шторма."))
    expanded=store.build_impact_package("tenant-a",first["event_id"],source_policy_id="policy-1",
        policy_version="v1",allowed_source_ids=["src-a","src-b","src-c"],llm=llm)
    assert llm.calls==1
    assert expanded.coverage.permitted_publications==2
    assert expanded.coverage.independent_origin_groups==1

    store.ingest(publication(source="src-c",uri="https://wire-c.example/shared",
        origin_group_id="wire-c",content="Публикация сообщает: порт закрыт на два дня из-за шторма."))
    independent=store.build_impact_package("tenant-a",first["event_id"],source_policy_id="policy-1",
        policy_version="v1",allowed_source_ids=["src-a","src-b","src-c"],llm=llm)
    assert llm.calls==2
    assert independent.coverage.permitted_publications==3
    assert independent.coverage.origin_group_count==2
    assert independent.coverage.independent_origin_groups==1
    assert independent.coverage.independence_status=="unknown"


def _multiprocess_package_build(path, event_id, counter_path):
    import fcntl
    from types import SimpleNamespace
    store=EventStore(path)
    class CountingLLM:
        config=SimpleNamespace(model="multiprocess-singleflight",max_output_tokens=256)
        def complete(self,*args,**kwargs):
            fd=os.open(counter_path,os.O_CREAT|os.O_RDWR,0o600)
            try:
                fcntl.flock(fd,fcntl.LOCK_EX)
                os.lseek(fd,0,os.SEEK_SET)
                current=os.read(fd,32).decode() or "0"
                os.lseek(fd,0,os.SEEK_SET)
                os.ftruncate(fd,0)
                os.write(fd,str(int(current)+1).encode())
                os.fsync(fd)
            finally:
                fcntl.flock(fd,fcntl.LOCK_UN)
                os.close(fd)
            time.sleep(0.15)
            return {"content":json.dumps({"claims":[],"entities":[],"territories":[],"resources":[],"mechanisms":[]})}
    store.build_impact_package("tenant-a",event_id,source_policy_id="policy-1",policy_version="v1",
        allowed_source_ids=["src-a"],llm=CountingLLM())


def test_shared_package_model_call_is_singleflight_across_processes(tmp_path):
    path=str(tmp_path/"events.sqlite3")
    store=EventStore(path)
    event=store.ingest(publication(content="Публикация сообщает: порт временно закрыт."))
    counter=str(tmp_path/"calls.txt")
    # `fork` keeps this offline test small; both workers still open independent
    # EventStore connections and acquire the process-level file lock.
    context=multiprocessing.get_context("fork")
    processes=[context.Process(target=_multiprocess_package_build,args=(path,event["event_id"],counter))
               for _ in range(2)]
    for process in processes: process.start()
    for process in processes: process.join(timeout=10)
    assert all(not process.is_alive() and process.exitcode==0 for process in processes)
    assert Path(counter).read_text()=="1"


def test_uncertain_extraction_reservation_never_blindly_retries(store):
    from types import SimpleNamespace
    first=store.ingest(publication(content="Публикация сообщает: порт закрыт."))
    snapshot=store.get_event("tenant-a",first["event_id"],source_policy_id="policy-1",
        policy_version="v1",allowed_source_ids=["src-a"])
    allowed=store._allowed("tenant-a","policy-1","v1",["src-a"])
    allowed_hash=__import__("hashlib").sha256(
        json.dumps([allowed,None],separators=(",",":")).encode()).hexdigest()
    semantic=store._semantic_signature(snapshot["sources"])
    model="crashed-after-send"
    reservation_id=__import__("hashlib").sha256("\0".join(("tenant-a",first["event_id"],"policy-1","v1",
        allowed_hash,semantic,model,"event-claims-v2")).encode()).hexdigest()
    with store._connect() as db:
        db.execute("INSERT INTO claim_extraction_reservations(tenant_id,reservation_id,status,created_at,lease_until,owner_token) VALUES(%s,%s,'started',%s,%s,%s)",
            ("tenant-a",reservation_id,"2026-10-24T00:00:00Z","2026-10-24T00:01:00Z","crashed-owner"))
    class NeverCall:
        config=SimpleNamespace(model=model,max_output_tokens=256)
        calls=0
        def complete(self,*args,**kwargs):
            self.calls+=1
            raise AssertionError("uncertain paid call was retried")
    llm=NeverCall()
    claims,metadata=store.extract_claims("tenant-a",first["event_id"],source_policy_id="policy-1",
        policy_version="v1",allowed_source_ids=["src-a"],llm=llm)
    assert claims and all(claim.kind=="reported_fact" for claim in claims)
    assert llm.calls==0
    assert "extractor_call_outcome_uncertain_no_automatic_retry" in metadata["limitations"]
    replay,replay_metadata=store.extract_claims("tenant-a",first["event_id"],source_policy_id="policy-1",
        policy_version="v1",allowed_source_ids=["src-a"],llm=llm)
    assert replay==claims
    assert replay_metadata==metadata
    assert llm.calls==0


def test_original_at_requested_event_revision_does_not_leak_future_correction(store):
    first=store.ingest(publication(uri="https://news.example/versioned",
        document_revision_ref="doc-original:1",content="Исходная публикация сообщает о закрытии порта."))
    pub_id=first["publication_id"]
    store.ingest(publication(uri="https://news.example/versioned",
        document_revision_ref="doc-original:2",content="Исправление: порт открыт и работает."))
    old=store.original("tenant-a",first["event_id"],pub_id,source_policy_id="policy-1",
        policy_version="v1",allowed_source_ids=["src-a"],revision=first["revision"])
    current=store.original("tenant-a",first["event_id"],pub_id,source_policy_id="policy-1",
        policy_version="v1",allowed_source_ids=["src-a"])
    assert old["content"]=="Исходная публикация сообщает о закрытии порта."
    assert old["document_revision_ref"]=="doc-original:1"
    assert current["content"]=="Исправление: порт открыт и работает."


def test_clone_snapshot_freezes_one_tenant_under_a_new_logical_namespace(store):
    first=store.ingest(publication(tenant="tenant-clone",uri="https://news.example/snapshot",
        document_revision_ref="doc-snapshot:1",content="Исходное состояние события."))
    namespace=store.namespace+":history:fixture"
    clone=store.clone_snapshot(namespace,"tenant-clone")
    snapshot=clone.get_event("tenant-clone",first["event_id"],source_policy_id="policy-1",
        policy_version="v1",allowed_source_ids=["src-a"])
    assert snapshot and snapshot["sources"][0]["content"]=="Исходное состояние события."
    store.ingest(publication(tenant="tenant-clone",uri="https://news.example/snapshot",
        document_revision_ref="doc-snapshot:2",content="Обновлённое состояние события."))
    assert clone.get_event("tenant-clone",first["event_id"],source_policy_id="policy-1",
        policy_version="v1",allowed_source_ids=["src-a"])["sources"][0]["content"]=="Исходное состояние события."
    reused=store.clone_snapshot(namespace,"tenant-clone")
    assert reused.get_event("tenant-clone",first["event_id"],source_policy_id="policy-1",
        policy_version="v1",allowed_source_ids=["src-a"])["sources"][0]["content"]=="Исходное состояние события."


def test_clone_snapshot_rejects_unmarked_populated_target(store):
    namespace=store.namespace+":history:occupied"
    target=EventStore(store.path,namespace=namespace)
    target.ingest(publication(tenant="tenant-clone",uri="https://news.example/unrelated",
        document_revision_ref="doc-unrelated:1",content="Не помеченное состояние."))
    with pytest.raises(ValueError,match="historical_snapshot_namespace_conflict"):
        store.clone_snapshot(namespace,"tenant-clone")


@pytest.mark.skipif(not os.getenv("INTELLIGENCE_MIGRATION_DATABASE_URL"),
                    reason="requires the isolated PostgreSQL migration test service")
def test_clone_snapshot_reuses_imported_historical_namespace_as_frozen(tmp_path,monkeypatch):
    import sqlite3
    from intelligence import sqlite_import

    state_root=tmp_path/"state"
    historical_dir=state_root/"historical-events"
    historical_dir.mkdir(parents=True)
    monkeypatch.setenv("INTELLIGENCE_STATE_DIR",str(state_root))
    signature="a"*64
    historical_path=historical_dir/f"{signature}.sqlite3"
    with sqlite3.connect(historical_path) as legacy:
        legacy.execute("CREATE TABLE events (tenant_id TEXT NOT NULL,event_id TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(tenant_id,event_id))")
        legacy.execute("INSERT INTO events VALUES (?,?,?)",("tenant-imported-history","event-frozen","2026-10-25T00:00:00Z"))
    sqlite_import.import_database(historical_path,"events",historical_path,confirm_quiesced=True)

    current=EventStore(state_root/"events.sqlite3")
    current.ingest(publication(tenant="tenant-imported-history",uri="https://news.example/current",
        document_revision_ref="doc-current:1",content="Текущая версия."))
    historical=EventStore(historical_path)
    reused=current.clone_snapshot(historical.namespace,"tenant-imported-history")
    with reused._connect() as db:
        event_ids=[row[0] for row in db.execute("SELECT event_id FROM events WHERE tenant_id=%s ORDER BY event_id",
            ("tenant-imported-history",)).fetchall()]
        marker=db.execute("SELECT snapshot_hash FROM snapshot_markers WHERE tenant_id=%s",
            ("tenant-imported-history",)).fetchone()
    assert event_ids==["event-frozen"]
    assert marker and marker["snapshot_hash"]
    current.ingest(publication(tenant="tenant-imported-history",uri="https://news.example/current-2",
        document_revision_ref="doc-current:2",content="Ещё более новая версия."))
    reused_again=current.clone_snapshot(historical.namespace,"tenant-imported-history")
    with reused_again._connect() as db:
        event_ids=[row[0] for row in db.execute("SELECT event_id FROM events WHERE tenant_id=%s ORDER BY event_id",
            ("tenant-imported-history",)).fetchall()]
    assert event_ids==["event-frozen"]


def test_allowed_document_revision_refs_scope_package_and_original_reads(store):
    first=store.ingest(publication(uri="https://news.example/doc-filter",
        document_revision_ref="doc-permitted:1",content="Материал в разрешённой ревизии."))
    second=store.ingest(publication(source="src-b",uri="https://wire.example/doc-filter",
        document_revision_ref="doc-outside:1",content="Другой материал с отличающимся содержанием и происхождением."))
    # Only the explicitly named revision may enter a package, even when both
    # publications share the same tenant and event query.
    package=store.build_impact_package("tenant-a",first["event_id"],source_policy_id="policy-1",
        policy_version="v1",allowed_source_ids=["src-a","src-b"],
        allowed_document_revision_refs=["doc-permitted:1"])
    assert [source.document_revision_ref for source in package.sources]==["doc-permitted:1"]
    assert "doc-outside:1" not in [source.document_revision_ref for source in package.sources]
    excluded=store.original("tenant-a",first["event_id"],second["publication_id"],source_policy_id="policy-1",
        policy_version="v1",allowed_source_ids=["src-a","src-b"],
        allowed_document_revision_refs=["doc-permitted:1"])
    assert excluded is None


def test_opinion_and_forecast_claim_kinds_are_not_collapsed_to_fact(store):
    first=store.ingest(publication(content="Эксперт считает, что засуха опасна. Издание прогнозирует рост цен."))
    class StubLLM:
        class Config: model="claim-type-model"; max_output_tokens=512
        config=Config()
        def complete(self,*args,**kwargs):
            return {"content":__import__("json").dumps({"claims":[
                {"kind":"fact","statement":"Эксперт высказал оценку.","quote":"Эксперт считает, что засуха опасна.","source_id":"src-a","publication_id":first["publication_id"]},
                {"kind":"fact","statement":"Издание прогнозирует рост цен.","quote":"Издание прогнозирует рост цен.","source_id":"src-a","publication_id":first["publication_id"]}]})}
    claims,_=store.extract_claims("tenant-a",first["event_id"],source_policy_id="policy-1",
        policy_version="v1",allowed_source_ids=["src-a"],llm=StubLLM())
    assert [claim.kind for claim in claims]==["opinion","forecast"]


def test_mechanisms_and_literal_semantics_are_validated_from_one_extraction(store):
    article="Компания СеверТранс сообщает о закрытии порта. Поставки топлива могут задержаться. " + ("дополнение " * 2000)
    first=store.ingest(publication(content=article))
    class SemanticLLM:
        class Config: model="semantic-model"; max_output_tokens=6000
        config=Config()
        calls=0
        def complete(self,messages,**kwargs):
            self.calls+=1
            assert kwargs["max_input_tokens"] == 24000
            assert kwargs["max_output_tokens"] == 2048
            payload=__import__("json").loads(messages[-1]["content"].split("Данные: ",1)[1])
            source=payload[0]
            assert source["publication_id"]==first["publication_id"]
            assert len(source["text"].encode("utf-8"))<=10000
            from intelligence.llm.service import estimate_text_tokens
            assert estimate_text_tokens(messages)<=kwargs["max_input_tokens"]
            return {"content":__import__("json").dumps({
                "claims":[{"kind":"reported_fact","statement":"Источник сообщает о закрытии порта.",
                    "quote":"Компания СеверТранс сообщает о закрытии порта.","source_id":"src-a",
                    "publication_id":first["publication_id"]}],
                "entities":["СеверТранс","выдуманная организация"],
                "territories":["порт"],"resources":["топлива"],
                "mechanisms":[
                    {"description":"Задержка топлива может повлиять на поставки компании.",
                     "source_publication_ids":[first["publication_id"]],"horizon":"пока действует закрытие",
                     "conditions":["Маршрут использует закрытый порт."],"alternatives":["Поставщик использует другой маршрут."],
                     "falsifiers":["Данные поставок не показывают задержку."],"status":"hypothesis"},
                    {"description":"Несуществующая публикация подтверждает механизм.",
                     "source_publication_ids":["pub_not_permitted"],"horizon":"неизвестно",
                     "conditions":["условие"],"alternatives":[],"falsifiers":["опровержение"],"status":"hypothesis"}]})}
    llm=SemanticLLM()
    package=store.build_impact_package("tenant-a",first["event_id"],source_policy_id="policy-1",
        policy_version="v1",allowed_source_ids=["src-a"],llm=llm)
    assert llm.calls==1
    assert package.entities==["СеверТранс"]
    assert package.territories==["порт"]
    assert package.resources==["топлива"]
    assert len(package.mechanisms)==1
    mechanism=package.mechanisms[0]
    assert mechanism.status=="hypothesis"
    assert mechanism.source_publication_ids==[first["publication_id"]]
    assert package.possible_mechanisms==[mechanism.description]
    assert "mechanism_reference_rejected" in package.coverage.limitations


def test_confidential_classification_blocks_llm_before_package_cache_or_call(store):
    first=store.ingest(publication(synthetic=False,data_classification="confidential"))
    class NeverCallLLM:
        class Config: model="guard-model"; max_output_tokens=512
        config=Config()
        calls=0
        def complete(self,*args,**kwargs):
            self.calls+=1
            raise AssertionError("confidential content must not be sent")
    llm=NeverCallLLM()
    # Prepopulate the package cache with the same key through local deterministic processing.
    store.build_impact_package("tenant-a",first["event_id"],source_policy_id="policy-1",
        policy_version="v1",allowed_source_ids=["src-a"],model_version="guard-model")
    with pytest.raises(ValueError,match="public_or_synthetic"):
        store.build_impact_package("tenant-a",first["event_id"],source_policy_id="policy-1",
            policy_version="v1",allowed_source_ids=["src-a"],llm=llm)
    with pytest.raises(ValueError,match="public_or_synthetic"):
        store.extract_claims("tenant-a",first["event_id"],source_policy_id="policy-1",
            policy_version="v1",allowed_source_ids=["src-a"],llm=llm)
    assert llm.calls==0


def test_extractor_error_is_cached_as_honest_fallback_metadata(store):
    first=store.ingest(publication())
    class FailingLLM:
        class Config: model="failing-semantic-model"; max_output_tokens=512
        config=Config()
        calls=0
        def complete(self,*args,**kwargs):
            self.calls+=1
            raise RuntimeError("private provider details are not persisted")
    llm=FailingLLM()
    claims,metadata=store.extract_claims("tenant-a",first["event_id"],source_policy_id="policy-1",
        policy_version="v1",allowed_source_ids=["src-a"],llm=llm)
    again,cached=store.extract_claims("tenant-a",first["event_id"],source_policy_id="policy-1",
        policy_version="v1",allowed_source_ids=["src-a"],llm=llm)
    assert llm.calls==1
    assert claims==again
    assert metadata==cached
    assert metadata["model_error"]=="RuntimeError"
    assert "semantic_extraction_failed" in metadata["limitations"]
    assert all(claim.kind=="reported_fact" for claim in claims)


def test_specific_same_day_incident_title_groups_opposing_reports_for_review(tmp_path):
    store=EventStore(tmp_path/'opposing.sqlite3')
    title='Закрыт экспортный терминал Северного причала'
    first=store.ingest(publication(title=title,content='Издание сообщает о закрытии терминала и задержке поставок.',document_revision_ref='doc-a@1'))
    counter=store.ingest(publication(source='src-b',uri='https://other.example/status',title=title,
        content='Оператор сообщает: терминал открыт, поставки идут штатно.',document_revision_ref='doc-b@1'))
    assert counter['event_id']==first['event_id']
    assert counter['grouping']=='matching_title_review_group'
    package=store.build_impact_package('tenant-a',first['event_id'],source_policy_id='policy',policy_version='1',allowed_source_ids=['src-a','src-b'])
    assert {claim.source_id for claim in package.claims}=={'src-a','src-b'}
    assert any('закрытии' in claim.quote for claim in package.claims)
    assert any('открыт' in claim.quote for claim in package.claims)
    assert package.coverage.independence_status=='unknown'
    replay=store.ingest(publication(title=title,content='Издание сообщает о закрытии терминала и задержке поставок.',document_revision_ref='doc-a@1'))
    assert replay['changed'] is False
    with store._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM publication_versions').fetchone()[0]==2
