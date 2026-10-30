import pytest

from moirai_agents.core import pipeline as P


def stage_pass(pid, name):
    v = P.Verdict(name)
    v.check("ok", True)
    P.finish_validation(pid, v)


def test_stages_of_each_pipeline():
    r = P.new("run", "app", "/src", "./run")
    a = P.new("analysis", "t", traces="/t")
    assert list(r["stages"]) == ["explore", "smoke", "annotate", "integrate", "moirai_smoke", "baseline", "query"]
    assert list(a["stages"]) == ["ingest", "dashboard", "query"]


def test_a_stage_passes_only_with_verdict_gate_and_feedback_resolved():
    pid = P.new("analysis", "t", traces="/t")["id"]
    with pytest.raises(P.PipelineError, match="verdict"):
        P.advance(pid, "ingest")
    stage_pass(pid, "ingest")
    with pytest.raises(P.PipelineError, match="gate"):
        P.advance(pid, "ingest")
    P.decide(pid, "ingest", True, "looks right")
    fb = P.feedback(pid, "ingest", "process", "also check spill files")
    with pytest.raises(P.PipelineError, match="feedback"):
        P.advance(pid, "ingest")
    P.feedback_resolve(pid, "ingest", fb["id"], "capability", "abc123")
    assert P.advance(pid, "ingest")["stages"]["ingest"]["status"] == "passed"


def test_changed_outputs_invalidate_the_verdict():
    pid = P.new("analysis", "t", traces="/t")["id"]
    stage_pass(pid, "ingest")
    P.decide(pid, "ingest", True, "ok")
    P.record(pid, "ingest", outputs={"files": 3})
    with pytest.raises(P.PipelineError, match="validate again"):
        P.advance(pid, "ingest")


def test_stages_run_in_order_and_gate_needs_user_words():
    pid = P.new("analysis", "t", traces="/t")["id"]
    stage_pass(pid, "dashboard")
    P.decide(pid, "dashboard", True, "ok")
    with pytest.raises(P.PipelineError, match="cannot start"):
        P.advance(pid, "dashboard")
    with pytest.raises(P.PipelineError, match="verbatim"):
        P.decide(pid, "ingest", True, " ")


def test_remember_feedback_becomes_a_lesson(call):
    pid = call("pipeline_new", kind="analysis", name="t", traces="/t")["id"]
    r = call("pipeline_feedback", pipeline=pid, stage="ingest", kind="remember", text="these traces are from the 2-node run")
    assert r["resolution"] == "lesson"
    from moirai_agents.core import capstore
    assert "2-node run" in capstore.get("trace_ingest")["lessons"]
    assert P.summary(P.load(pid))["stages"][0]["feedback_open"] == 0


def failing(pid, *names):
    v = P.Verdict("ingest")
    for n in names:
        v.check(n, False, "bad", "fix it")
    return P.finish_validation(pid, v)


def test_failed_verdict_routes_to_the_corrector_and_progress_earns_attempts():
    pid = P.new("analysis", "t", traces="/t")["id"]
    r = failing(pid, "a", "b")
    assert r["corrector"] == {"skill": "trace_ingest", "state": "correcting", "count": 1, "budget": 3}
    assert "section Corrector" in r["next"]
    r = failing(pid, "a")                            # fewer failures: closer
    assert r["corrector"]["budget"] == 5 and r["corrector"]["state"] == "correcting"


def test_the_same_failure_twice_stops_the_loop():
    pid = P.new("analysis", "t", traces="/t")["id"]
    failing(pid, "a")
    r = failing(pid, "a")
    assert r["corrector"]["state"] == "stalled" and "stop correcting" in r["next"]


def test_failed_assumption_is_inconclusive_not_a_failure():
    pid = P.new("analysis", "t", traces="/t")["id"]
    v = P.Verdict("ingest")
    v.assume("dependency", False, "not installed")
    r = P.finish_validation(pid, v)
    assert r["status"] == "inconclusive" and not r["failed"]
    with pytest.raises(P.PipelineError, match="inconclusive"):
        P.advance(pid, "ingest")


def test_every_capability_declares_its_loop():
    from moirai_agents.core import capstore
    for n, (_, d, m) in capstore.all_caps().items():
        assert m["pvc"]["predictor"] and m["pvc"]["validator"], n
        assert not capstore.check_pvc(m["pvc"], m["tools"]), n


def test_meta_validators(call):
    pid = call("pipeline_new", kind="analysis", name="t", traces="/t")["id"]
    assert call("pipeline_validate", pipeline=pid)["status"] == "pass"
    P.update(pid, lambda c: c["stages"]["ingest"].update(status="passed"))
    r = call("pipeline_validate", pipeline=pid)
    assert r["status"] == "fail" and {"ingest:verdict", "ingest:gate"} <= {f["name"] for f in r["failed"]}
    assert call("lesson_validate")["status"] == "pass"
    assert call("df_validate")["status"] in ("inconclusive", "pass")
