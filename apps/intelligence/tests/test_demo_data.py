from intelligence.demo_data import run_demo


def test_offline_demo_runs_fixture_collection_events_and_two_company_baseline(tmp_path):
    result = run_demo(tmp_path / "demo-state")
    summary = result.coverage["demo_summary"]

    assert summary["mode"] == "offline_synthetic_fixture"
    assert summary["network_access"] is False
    assert summary["fetch_calls"] == 2
    assert summary["events"] == len(result.events) == 2
    assert summary["companies"] == 2
    assert summary["candidates"] == len(result.candidates) == 4
    assert len(summary["configuration_refs"]) == 2
    assert all(candidate.execution_mode == "baseline" for candidate in result.candidates)
    assert all(event["data_classification"] == "synthetic" for event in result.events)
    assert all(row["raw_ref"] for row in result.collection["documents"])


def test_new_business_demo_cli_preserves_all_activities_and_attributed_sources(tmp_path):
    from intelligence.cli import main
    import json
    output=tmp_path/'result.json'
    assert main(['demo-business','--state-dir',str(tmp_path/'state'),'--output',str(output)])==0
    result=json.loads(output.read_text())
    assert result['coverage']['demo_summary']['candidates']==4
    assert result['coverage']['model_calls']==0
    assert result['coverage']['demo_summary']['source_network_access'] is False
    assert any(item['company_id']=='sensorworks' and item['status']=='signal_found' for item in result['selections'])
    assert all(item['status']=='no_signal_found' for item in result['selections'] if item['company_id']=='softnorth')
