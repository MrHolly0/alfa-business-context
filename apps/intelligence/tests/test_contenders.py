from intelligence.analysts import AnalystConfig, builtin_configs
from intelligence.contenders import CandidateGenerator, PairJudge, critic_round

def test_contenders_exports_shared_experiment_contract():
    base = builtin_configs()[0]
    assert isinstance(base, AnalystConfig)
    assert len(CandidateGenerator().generate(base=base, count=2)) == 2
    assert PairJudge().judge(case={}, a={}, b={}, orientation="forward")["advisory"] is True
    assert callable(critic_round)
