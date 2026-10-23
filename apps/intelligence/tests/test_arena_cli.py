import json
from pathlib import Path

from intelligence.arena_cli import main
from intelligence.cli import make_pipeline


def test_offline_tournament_and_explicit_analysis_selection(tmp_path, capsys):
    state = str(tmp_path/'state')
    assert main(['--state-dir',state,'list']) == 0
    assert 'baseline@1' in capsys.readouterr().out
    output = tmp_path/'report.json'
    assert main(['--state-dir',state,'run','--run-id','smoke','--output',str(output)]) == 0
    report = json.loads(output.read_text())
    assert report['winner_ref'] == 'baseline@1'
    assert report['promotion_eligible'] is False
    assert report['synthetic'] is True
    assert report['holdout']['passed'] is True
    assert main(['--state-dir',state,'promote','--run-id','smoke','--reviewer','team','--scope','impact:any:*','--expected-revision','0']) == 2
    store, pipeline = make_pipeline(tmp_path/'other',analyst='procurement@1')
    request = json.loads((Path(__file__).parents[1]/'examples/analysis-request.json').read_text())
    assert pipeline.run(request).versions.analyst_config == 'procurement@1'


def test_generated_offline_configs_register_with_shared_schema(tmp_path,capsys):
    assert main(['--state-dir',str(tmp_path),'generate']) == 0
    result=json.loads(capsys.readouterr().out)
    assert len(result['registered']) == 3
    assert result['costs'] == []


def test_provider_diagnostic_contains_code_without_secret(tmp_path,capsys,monkeypatch):
    from intelligence.contenders import CandidateGenerator
    from intelligence.llm.types import ProviderError
    def fail(*args,**kwargs):
        raise ProviderError('missing_api_key','secret-do-not-print')
    monkeypatch.setattr(CandidateGenerator,'generate',fail)
    assert main(['--state-dir',str(tmp_path),'generate']) == 2
    error=capsys.readouterr().err
    assert 'missing_api_key' in error and 'secret-do-not-print' not in error
