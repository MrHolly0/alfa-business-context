"""Cross-process registry CAS tests against the isolated PostgreSQL namespace."""
import multiprocessing

from intelligence.analysts import AnalystConfig, PromptRegistry
from test_analysts import eligible_report


def _publish_worker(path, scope, candidate_ref, reviewer, report, expected_revision, barrier, results):
    registry=PromptRegistry(path)
    barrier.wait(timeout=20)
    try:
        registry.promote(scope=scope,ref=candidate_ref,report=report,reviewer=reviewer,
                         expected_revision=expected_revision)
        results.put(("published",reviewer))
    except ValueError as exc:
        if "activation revision conflict" in str(exc) or "active baseline changed" in str(exc):
            results.put(("conflict",reviewer))
        else:
            results.put(("unexpected_error",type(exc).__name__,str(exc)))


def _rollback_worker(path, scope, reviewer, expected_revision, barrier, results):
    registry=PromptRegistry(path)
    barrier.wait(timeout=20)
    try:
        registry.rollback(scope=scope,reviewer=reviewer,expected_revision=expected_revision)
        results.put(("rolled_back",reviewer))
    except ValueError as exc:
        if "activation revision conflict" in str(exc):
            results.put(("conflict",reviewer))
        else:
            results.put(("unexpected_error",type(exc).__name__,str(exc)))


def _race(target, worker_args):
    context=multiprocessing.get_context("spawn")
    barrier=context.Barrier(3)
    results=context.Queue()
    processes=[context.Process(target=target,args=(*args,barrier,results)) for args in worker_args]
    for process in processes: process.start()
    barrier.wait(timeout=20)
    messages=[results.get(timeout=30) for _ in processes]
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode==0
    results.close()
    return messages


def test_independent_processes_compare_and_swap_same_publication_revision(tmp_path):
    path=str(tmp_path/"registry.pg")
    registry=PromptRegistry(path)
    scope="impact:any:*"
    candidate=AnalystConfig(config_id="process-candidate",role_scope="any")
    registry.register(candidate)
    # This is the established eligible-report fixture; the test checks only the
    # publication CAS and does not claim that its test-double evidence is quality proof.
    base=eligible_report(registry,scope,candidate.ref)
    reports=[]
    for reviewer in ("reviewer-a","reviewer-b"):
        report={**base,"human_review":{"approved":True,"reviewer":reviewer}}
        reports.append((path,scope,candidate.ref,reviewer,report,0))

    outcomes=_race(_publish_worker,reports)

    assert sorted(item[0] for item in outcomes)==["conflict","published"]
    snapshot=registry.snapshot(scope)
    assert snapshot["activation_revision"]==1
    assert snapshot["ref"]==candidate.ref
    with registry._db() as db:
        rows=db.execute("SELECT revision,ref,action FROM activations WHERE scope=%s ORDER BY revision",(scope,)).fetchall()
    assert len(rows)==1 and rows[0]["revision"]==1 and rows[0]["ref"]==candidate.ref
    assert rows[0]["action"]=="promote"


def test_independent_processes_append_only_one_atomic_rollback(tmp_path):
    path=str(tmp_path/"registry.pg")
    registry=PromptRegistry(path)
    scope="impact:any:*"
    candidate=AnalystConfig(config_id="rollback-candidate",role_scope="any")
    registry.register(candidate)
    report=eligible_report(registry,scope,candidate.ref)
    registry.promote(scope=scope,ref=candidate.ref,report=report,reviewer="alice",
                     expected_revision=0)

    outcomes=_race(_rollback_worker,[
        (path,scope,"rollback-a",1),
        (path,scope,"rollback-b",1),
    ])

    assert sorted(item[0] for item in outcomes)==["conflict","rolled_back"]
    snapshot=registry.snapshot(scope)
    assert snapshot["activation_revision"]==2
    assert snapshot["ref"]=="baseline@1"
    with registry._db() as db:
        rows=db.execute("SELECT revision,ref,action FROM activations WHERE scope=%s ORDER BY revision",(scope,)).fetchall()
    assert [(row["revision"],row["ref"],row["action"]) for row in rows]==[
        (1,candidate.ref,"promote"),(2,"baseline@1","rollback")]
