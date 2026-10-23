"""Paid development configuration stays explicit and uses its own currency reservation."""
from decimal import Decimal
from types import SimpleNamespace

import pytest

from intelligence.cli import APP_ROOT,make_pipeline
from intelligence.llm.config import load_config
from intelligence.artifacts import ArtifactStore
from intelligence.worker import Worker,InvalidJob
from test_worker_runtime import job


def test_paid_template_enables_bounded_pipeline_without_sending(tmp_path,monkeypatch):
    monkeypatch.delenv('OPENROUTER_API_KEY',raising=False)
    _, pipeline=make_pipeline(tmp_path,mode='model',config=APP_ROOT/'examples/providers.paid.template.json',profile='openrouter')
    config=pipeline.llm.config
    assert config.model=='google/gemma-4-26b-a4b-it'
    assert config.allow_paid is True
    assert config.daily_limit==Decimal('.20')
    assert config.max_request_cost==Decimal('.01')
    assert config.input_per_million==Decimal('.06')
    assert config.output_per_million==Decimal('.33')


def test_paid_worker_never_uses_rub_reservation_as_usd(tmp_path):
    config=load_config(APP_ROOT/'examples/providers.paid.template.json',profile='openrouter')
    pipeline=SimpleNamespace(llm=SimpleNamespace(config=config))
    worker=Worker(None,pipeline,ArtifactStore(tmp_path/'artifact.db'),tenant_ids={'tenant-a'})
    value=job()
    value['budget']['cost_reservation_rub']=500
    with pytest.raises(InvalidJob,match='USD'): worker._check(value)
    value['budget']['cost_reservation_usd']=.001
    with pytest.raises(InvalidJob,match='reservation'): worker._check(value)
    value['budget']['cost_reservation_usd']=.01
    request,_,_=worker._check(value)
    assert request.tenant_id=='tenant-a'
