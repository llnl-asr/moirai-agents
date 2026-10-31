"""Manual check against a REAL moirai: the run pipeline's moirai stages, tailing the trace while the service writes it.

    MOIRAI_AGENTS_MOIRAI_PYTHON=<venv>/bin/python python3 tests/live_moirai.py <scratch dir>
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
work = Path(sys.argv[1])
work.mkdir(parents=True, exist_ok=True)
os.environ.update(MOIRAI_AGENTS_PIPELINES_DIR=str(work / "pipelines"), MOIRAI_AGENTS_LEARNING_DIR=str(work / "learning"),
                  MOIRAI_AGENTS_USER_CAPABILITIES=str(work / "ucaps"), MOIRAI_AGENTS_PROJECT_CAPABILITIES=str(work / "pcaps"))
from moirai_agents.core import capstore, jobs, pipeline as P, provtrace as T  # noqa: E402

t = {}
capstore.refresh(t, force=True)
call = lambda n, **k: t[n]["fn"](**k)  # noqa: E731
src = work / "app"
src.mkdir(exist_ok=True)
(src / "app.py").write_text('''import time

def step(i, prev):
    time.sleep(0.2)
    return f"chunk{i}"

if __name__ == "__main__":
    prev = "seed"
    for i in range(25):
        prev = step(i, prev)
''')
pid = call("pipeline_new", kind="run", name="live", source=str(src), command="python app.py")["id"]
for s in ("plan", "explore", "smoke", "discover"):
    P.update(pid, lambda c, s=s: c["stages"][s].update(status="passed"))
spec = {"scientific_artifacts": ["chunk"], "entity_types": [{"type": "chunk", "role": "output", "description": "a chunk of the demo output", "id_scheme": "chunk<i>"}],
        "activities": [{"file": "app.py", "function": "step", "name": "step", "used": [{"type": "chunk", "key_expr": "prev"}], "generated": [{"type": "chunk", "key_expr": "result"}]}]}
print(call("annotate_spec", pipeline=pid, spec=spec))
print({k: v for k, v in call("annotate_apply", pipeline=pid).items() if k != "diff"})
P.update(pid, lambda c: c["stages"]["annotate"].update(status="passed"))
print(call("integrate_apply", pipeline=pid))
print("integrate:", call("integrate_validate", pipeline=pid)["status"])
P.update(pid, lambda c: c["stages"]["integrate"].update(status="passed"))
r = call("moirai_smoke_run", pipeline=pid, wait=0)
print(r["status"], r["traces"])
tail, g, seen = T.Tail(r["traces"]), T.Graph(), []
while True:
    g.extend(tail.poll())
    n = g.counts()
    seen.append(n["activities"])
    print(f"t={len(seen) * 0.5:4.1f}s activities={n['activities']} entities={n['entities']} files={len(tail.pos)} stalled={tail.stalled}", flush=True)
    if jobs.get(pid, r["job"], 0)["status"] != "running":
        break
    time.sleep(0.5)
time.sleep(1)
g.extend(tail.poll())
print("final", g.counts())
print("verdict", json.dumps({k: v for k, v in call("moirai_validate", pipeline=pid, stage="moirai_smoke").items() if k in ("status", "failed", "unknown")})[:600])
print(jobs.get(pid, r["job"], 1500).get("log_tail", "")[-800:])
print("LIVE" if any(0 < x < g.counts()["activities"] for x in seen) else "NOT LIVE: nothing was visible before the end")
