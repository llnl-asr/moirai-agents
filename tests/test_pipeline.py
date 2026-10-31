import time

import pytest

from moirai_agents.core import pipeline as P


def planned(pid):
    P.update(pid, lambda c: c["stages"]["plan"].update(status="passed"))


def stage_pass(pid, name):
    v = P.Verdict(name)
    v.check("ok", True)
    P.finish_validation(pid, v)


def test_stages_of_each_pipeline():
    r = P.new("run", "app", "/src", "./run")
    a = P.new("analysis", "t", traces="/t")
    assert list(r["stages"]) == ["plan", "explore", "smoke", "discover", "annotate", "integrate", "moirai_smoke", "baseline", "graph", "query", "package"]
    assert list(a["stages"]) == ["plan", "ingest", "dashboard", "query"]


def test_a_stage_passes_only_with_verdict_gate_and_feedback_resolved():
    pid = P.new("analysis", "t", traces="/t")["id"]
    planned(pid)
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
    planned(pid)
    stage_pass(pid, "ingest")
    P.decide(pid, "ingest", True, "ok")
    P.record(pid, "ingest", outputs={"files": 3})
    with pytest.raises(P.PipelineError, match="validate again"):
        P.advance(pid, "ingest")


def test_stages_run_in_order_and_gate_needs_user_words():
    pid = P.new("analysis", "t", traces="/t")["id"]
    planned(pid)
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


def test_advance_repeats_the_standing_rule(call):
    from moirai_agents.core import capstore
    pid = call("pipeline_new", kind="analysis", name="t", traces="/t")["id"]
    P.finish_validation(pid, P.Verdict("plan"))
    P.record(pid, "plan", status="needs_user")
    call("pipeline_decide", pipeline=pid, stage="plan", approved=True, user_answer="yes")
    assert call("pipeline_advance", pipeline=pid, stage="plan")["standing"] == capstore.STANDING


def test_a_wall_of_text_is_refused_before_it_reaches_the_user(call):
    pid = call("pipeline_new", kind="analysis", name="t", traces="/t")["id"]
    wall = "word " * 400
    for args in ({"question": wall}, {"question": "ok?", "context": wall}, {"question": "ok?", "context": "x" * 300}):
        r = call("ask_user", pipeline=pid, stage="plan", **args)
        assert r["status"] == "error" and P.load(pid)["stages"]["plan"]["status"] != "needs_user"
    r = call("ask_user", question="Approve annotate?", context="**Done**: 24 of 25 checks passed.\n- one inconclusive", details=wall, pipeline=pid, stage="plan")
    assert r["details"] == wall and P.load(pid)["stages"]["plan"]["status"] == "needs_user"


def test_scheduler_jobs_land_in_their_group():
    from moirai_agents.core import jobs as J
    row = lambda st, res="": J._flux_row(f"f1|{st}|{res}|run.sh|pdebug|2|1700000000.5|12.5|1700000010.0|1700000020.0")
    assert [row(s)["group"] for s in ("DEPEND", "PRIORITY", "SCHED", "RUN", "CLEANUP")] == ["sched", "sched", "sched", "run", "run"]
    assert row("INACTIVE", "COMPLETED")["group"] == "ok"
    assert [row("INACTIVE", r)["group"] for r in ("FAILED", "CANCELED", "TIMEOUT")] == ["bad", "bad", "bad"]
    assert row("RUN")["nodes"] == "2" and J._flux_row("not a job") is None


def test_a_process_says_what_ran_and_what_it_wrote(call):
    from moirai_agents.core import jobs as J
    pid = call("pipeline_new", kind="analysis", name="t", traces="/t")["id"]
    j = J.start(pid, "ingest", "demo", "echo hello from the job\nexit 3\n", "/tmp")
    J.wait(pid, j["id"], 10)
    d = J.detail(pid, j["id"])
    assert "echo hello" in d["command"] and "hello from the job" in d["output"] and d["rc"] == 3 and not d["running"]


def test_a_batch_job_shows_the_file_it_wrote_to(tmp_path):
    from moirai_agents.core import jobs as J
    f = tmp_path / "run.out"
    f.write_text("predicted 2 structures\n")
    out = J._redirected(f"[0]: stdout redirected to {f}\n[0]: stderr redirected to {f}\n[1]: stdout redirected to /nonexistent/x\n")
    assert out.count("predicted 2 structures") == 1 and "/nonexistent/x: No such file" in out


def test_a_scheduler_job_id_is_checked():
    from moirai_agents.core import jobs as J
    assert J.flux_detail("../etc; rm")["status"] == "error"


def test_scheduler_jobs_come_latest_update_first(monkeypatch):
    from moirai_agents.core import jobs as J
    line = lambda jid, sub, run, end: J._flux_row(f"{jid}|INACTIVE|COMPLETED|x|q|1|{sub}|1|{run}|{end}")
    monkeypatch.setitem(J._flux_cache, "at", time.time())
    monkeypatch.setitem(J._flux_cache, "rows", [line("old", 10, 11, 12), line("late", 1, 2, 90), line("queued", 50, 0, 0)])
    assert [j["id"] for j in J.submitted()] == ["late", "queued", "old"]
    assert [j["id"] for j in J.submitted(since=5)] == ["queued", "old"]


def test_no_scheduler_means_no_submitted_jobs(monkeypatch):
    from moirai_agents.core import jobs as J
    monkeypatch.setitem(J._flux_cache, "at", 0.0)
    monkeypatch.setenv("PATH", "/nonexistent")
    assert J.submitted() == []


def at_gate(call, monkeypatch, on=True):
    monkeypatch.setenv("MOIRAI_AGENTS_WEBUI_SESSION", "chat1")
    pid = call("pipeline_new", kind="analysis", name="t", traces="/t")["id"]
    P.set_auto_mode("chat1", on)
    return pid


def passed(pid, stage="plan"):
    P.finish_validation(pid, P.Verdict(stage))


def test_auto_mode_takes_the_recommended_option_at_a_gate(call, monkeypatch):
    pid = at_gate(call, monkeypatch)
    passed(pid)
    r = call("ask_user", question="Go on?", pipeline=pid, stage="plan", options=[{"label": "Approve", "recommended": True}, {"label": "Redo"}])
    assert r["status"] == "answered by auto mode" and r["answer"] == "Approve"
    gate = P.stage(P.load(pid), "plan")["gate"]
    assert gate["approved"] and gate["answer"].startswith("Auto mode")
    assert call("pipeline_advance", pipeline=pid, stage="plan")["status"] == "ok"


def test_auto_mode_approves_a_passing_gate_with_no_options(call, monkeypatch):
    pid = at_gate(call, monkeypatch)
    passed(pid)
    assert call("ask_user", question="Go on?", pipeline=pid, stage="plan")["auto"]


def test_auto_mode_answers_a_plain_question_without_a_gate(call, monkeypatch):
    at_gate(call, monkeypatch)
    r = call("ask_user", question="Which?", options=[{"label": "A"}, {"label": "B", "recommended": True}])
    assert r["answer"] == "B"
    assert call("ask_user", question="Sure?", kind="confirm")["answer"] == "Yes, go ahead"


@pytest.mark.parametrize("args", [
    {"question": "Which?", "options": [{"label": "A"}, {"label": "B"}]},
    {"question": "What is the path?", "kind": "freeform"},
    {"question": "Which?", "options": [{"label": "A", "recommended": True}, {"label": "B", "recommended": True}]},
])
def test_auto_mode_waits_when_it_has_no_single_recommendation(call, monkeypatch, args):
    at_gate(call, monkeypatch)
    assert call("ask_user", **args)["status"] == "question shown to the user"


def test_auto_mode_stops_at_a_gate_that_did_not_pass(call, monkeypatch):
    pid = at_gate(call, monkeypatch)
    v = P.Verdict("plan")
    v.check("thing", False, "broken", "fix it")
    P.finish_validation(pid, v)
    r = call("ask_user", question="Go on?", pipeline=pid, stage="plan", options=[{"label": "Approve", "recommended": True}])
    assert r["status"] == "question shown to the user" and not (P.stage(P.load(pid), "plan").get("gate") or {}).get("approved")


def test_auto_mode_stops_when_the_recommendation_is_to_stop(call, monkeypatch):
    pid = at_gate(call, monkeypatch)
    passed(pid)
    r = call("ask_user", question="Go on?", pipeline=pid, stage="plan", options=[{"label": "Stop here", "recommended": True}, {"label": "Go on"}])
    assert r["status"] == "question shown to the user"


def test_without_auto_mode_every_question_waits(call, monkeypatch):
    pid = at_gate(call, monkeypatch, on=False)
    passed(pid)
    r = call("ask_user", question="Go on?", pipeline=pid, stage="plan", options=[{"label": "Approve", "recommended": True}])
    assert r["status"] == "question shown to the user"


def test_auto_mode_is_per_chat_and_can_be_turned_off(call, monkeypatch):
    at_gate(call, monkeypatch)
    assert P.auto_mode("chat1") and not P.auto_mode("chat2")
    P.set_auto_mode("chat1", False)
    assert not P.auto_mode("chat1")
    with pytest.raises(P.PipelineError):
        P.set_auto_mode("../x", True)


def test_stage_state_says_what_happened(call):
    pid = call("pipeline_new", kind="analysis", name="t", traces="/t")["id"]
    P.record(pid, "ingest", status="running", outputs={"traces": "/w/pipe/traces/ingest/raw", "command": "run"}, note="reading traces")
    s = P.stage_state(P.load(pid), "ingest")
    assert s["command"] == "run" and [h["note"] for h in s["history"]] == ["reading traces"]
    assert s["checks"]["status"] is None and s["gate"] is None
    with pytest.raises(P.PipelineError):
        P.stage_state(P.load(pid), "nope")
    assert P.traces_run("/w/pipe/traces/ingest/raw", "/w/x/y") != "" and P.traces_run(None) == ""


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
    planned(pid)
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


STEPS = {s: {"intent": f"do {s}", "done_when": "it is done"} for s in ("plan", "ingest", "dashboard", "query")}


def test_a_plan_covers_every_stage_and_tracks_the_run(call, tmp_path):
    pid = call("pipeline_new", kind="analysis", name="t", traces="/t")["id"]
    assert "no step for stage query" in str(call("plan_write", pipeline=pid, goal="g", steps={"plan": STEPS["plan"]}))
    assert call("plan_write", pipeline=pid, goal="See what the run made", steps=STEPS)["status"] == "ok"
    assert call("plan_validate", pipeline=pid)["status"] == "pass"
    P.decide(pid, "plan", True, "ok")
    assert call("pipeline_advance", pipeline=pid, stage="plan")["next"] == "ingest"
    P.record(pid, "ingest", status="running", outputs={"summary": "3 files"})
    plan = call("plan_show", pipeline=pid)
    assert plan["steps"]["ingest"]["status"] == "running" and plan["steps"]["ingest"]["result"] == "3 files"
    text = (P.pdir(pid) / "PLAN.md").read_text()
    assert "### ingest (running)" in text and "3 files" in text and "See what the run made" in text


def test_a_failure_makes_the_plan_stale_until_it_is_revised(call):
    pid = call("pipeline_new", kind="analysis", name="t", traces="/t")["id"]
    call("plan_write", pipeline=pid, goal="g", steps=STEPS)
    planned(pid)
    v = P.Verdict("ingest")
    v.check("files", False, "none")
    P.finish_validation(pid, v)
    assert "validator failed: files" in call("plan_show", pipeline=pid)["steps"]["ingest"]["stale"]
    stage_pass(pid, "ingest")
    P.decide(pid, "ingest", True, "ok")
    with pytest.raises(P.PipelineError, match="plan is out of date"):
        P.advance(pid, "ingest")
    assert call("plan_update", pipeline=pid, stage="ingest", why="the traces were in a subfolder", approach="use the subfolder")["status"] == "ok"
    assert P.advance(pid, "ingest")["stages"]["ingest"]["status"] == "passed"
    plan = call("plan_show", pipeline=pid)
    assert plan["steps"]["ingest"]["approach"] == "use the subfolder" and plan["revisions"][0]["why"].startswith("the traces")
    assert "the traces were in a subfolder" in (P.pdir(pid) / "PLAN.md").read_text()


def test_process_feedback_makes_the_plan_stale(call):
    pid = call("pipeline_new", kind="analysis", name="t", traces="/t")["id"]
    call("plan_write", pipeline=pid, goal="g", steps=STEPS)
    call("pipeline_feedback", pipeline=pid, stage="query", kind="process", text="also compare two runs")
    assert "also compare" in call("plan_show", pipeline=pid)["steps"]["query"]["stale"]
