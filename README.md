# moirai-agents

Capability-centric agents for [moirai](https://github.com/llnl-asr/moirai) data provenance: annotate an application, run it, watch its provenance live, and explore it, with a person approving every step. It builds on [dftracer-agents](https://github.com/llnl-asr/dftracer-agents) and follows the capability model of Phronix.

## Everything is a capability

A capability is one folder in `src/moirai_agents/.agents/capabilities/<name>/`. The kernel has no tools of its own.

The standing rule: anything explored becomes a capability, constantly. A new workload, software, tool or system met in any stage is captured as `workload_<x>`, `software_<x>`, `tools_<x>` or `system_<x>`, or extends one that exists. The rule is `capstore.STANDING`: every chat prompt, rendered agent and `CLAUDE.md` carries it, `pipeline_advance` repeats it after each stage, and a skill whose Self-learn section omits it is refused. These families and their skill shape match dftracer-agents; `capability_builder` has the template.

`knowledge_search` is the first place to look. It walks one graphify graph over every capability, lesson and memory, and dftracer-agents' workload, software, system and tools skills and memories, then reads lessons and memory directly, because the graph matches headings and symbols rather than body text. The graph rebuilds itself when anything it covers changes. When a tool fails, its result carries `known_lessons`, the closest lessons found through the graph, including the lesson sections of dftracer-agents' skills, so an agent reads what is known before exploring. A `UserPromptSubmit` hook puts the best rows not yet shown in the session in front of each prompt. `memory_write` keeps what is true here, in `~/.moirai-agents/learning/memory/`.

| File | Holds |
|---|---|
| `tool.py` | the MCP tools: predictors, and the validators that rule on what they produced |
| `SKILL.md` | Rules, Constraints, Corner cases, Corrector, Self-heal, Self-learn |
| `agent.json` | the background agent: its fixed scope of tools, skills, permissions and level |
| `checks.json` | known input to expected output, run in a sandbox on every change |
| `capability.json` | version, stages, history, and `pvc`: which tools predict and which validate |
| `LESSONS.md` | what use has taught it |

Every capability is a **predictor, validator, corrector** loop. A tool predicts. A separate deterministic tool validates. On failure, the verdict sends the agent to the skill's Corrector section, which repairs the original tool or its input; then it predicts and validates again. Attempts follow measured progress: three to start, more while the failures change, and the same failure twice stops the loop for the user. A bundle that does not declare the loop is refused.

Lessons and capabilities go through the same review, in their panels: accept, reject, or refine by editing them or asking the agent. A refused edit changes nothing, and a correction to an applied lesson or a live capability takes effect only once it is accepted.

The web server looks for overlapping or conflicting lessons and capabilities in the background, and shows a **consolidate** marker next to the panel's title. Clicking it starts the work in its own chat on the server's node. Merges and changes are proposals, and contradictions go to the user.

`capability_builder` is the capability that builds capabilities: `capability_new` and `capability_update` validate a proposal and run its checks in a sandbox, a person approves, and it goes live with no restart.

Layers, lowest first: bundled, user (`~/.moirai-agents/capabilities`), project (`capabilities/` in this repo). A higher layer shadows a lower one wholesale, and the bundled layer is never written.

## The pipelines

Each stage has one owning capability and stops at a user gate.

| Pipeline | Stages |
|---|---|
| run | plan, explore, smoke, discover, annotate, integrate, moirai_smoke, baseline, graph, query, package |
| analysis | plan, ingest, dashboard, query |

Planning comes first: `PLAN.md`, in the session, has a step per stage. The engine syncs each stage's status and result into it as the run goes, and a failed verdict or process feedback marks a step stale until the plan says what changed. The last stage writes `final_provenance/`, a folder that reproduces the run for someone else, verified from a fresh copy in a clean login shell.

At a gate the user can approve, give **process** feedback (becomes a new or updated capability, which they approve) or say **remember** (becomes a lesson of that capability at once). A stage passes only with a passing verdict of its current outputs, an approval, and all feedback resolved.

## Web UI

```bash
scripts/setup_env.sh          # .venv, with dftracer-agents editable
$EDITOR .env                  # MOIRAI_AGENTS_MOIRAI_PYTHON, model access
scripts/webui_start.sh        # prints your link and the tunnel command
scripts/webui_stop.sh         # stops it and everything it started
scripts/webui_kill.sh         # stops your servers on every login node; --list only shows them
```

You run it yourself, as yourself: there are no accounts and no sign-in. Open the printed link; its private token only keeps other people on a shared login node out of an agent running as you.

Chats on the left, Chat and Dashboard in the middle, and Steps, Agents, Jobs, Lessons and Capabilities on the right. Each step is one row: its name opens what happened in it, and a step with traces links to their behaviors, runtime, temporal graph and knowledge graph in the Dashboard. Models: Claude, OpenAI or LivAI, each verified before it is saved. Every chat runs Claude Code in your workspace.

**The Dashboard is [dfdashboard](https://github.com/llnl-asr/dfdashboard).** The web UI starts it on first use, on a private port, and forwards `/dash/` to it, so there is one URL and one tunnel, and it stops with the server. Its Provenance view follows a run while `moirai-service` writes the trace: time runs top to bottom, a bar on the right describes what you select, and you can pick entities by type and show or hide activities, entity types and I/O. To look at any traces, paste a directory, a `.pfw.gz` file or a glob into the box at the top of the tab: they are linked, not copied, and a folder still being written stays live.

**It refreshes itself.** A capability's `tool.py` reloads in the MCP server, which tells the client. A change to the kernel re-executes the MCP server in place, with its connection intact. In the web UI, `api.py`, `prompts.py`, `core/` and static files reload during a chat, and open pages follow. `runtime.py`, `dfdash.py` and `server.py` re-execute on the same socket once no chat is running, and logins survive. An edit that does not compile is refused and shown on the page; the running code stays.

**It does not outlive its use.** After `MOIRAI_AGENTS_WEBUI_IDLE_S` (2 h) with no user action, chat or job, and in any case after `MOIRAI_AGENTS_WEBUI_MAX_S` (12 h), it stops its chats, sweeps every process it started and exits. Page polling does not count as activity. Background jobs have their own limit (`limit`, default `MOIRAI_AGENTS_JOB_LIMIT_S`, 1 h); on expiry they get SIGTERM, so their traps stop `moirai-service`.

## dftracer-agents

`scripts/setup_env.sh` installs it editable. The workspace uses its skills and agents, its permission policy, and its MCP server beside ours. A run pipeline owns one dftracer session from `pipeline_new`: the app is in `<session>/source`, and provenance work (the annotated copy, spec, traces, graph) in `<session>/provenance`, beside the session's own folders. Sessions live under `<workspace>/workspaces/`, the path that policy allows, whatever `DFTRACER_WORKSPACES` is set to in your shell. `smoke_session` runs dftracer's build and smoke tools there, and `df_call` reaches any other. An analysis pipeline has no app to build, so it has no session. Without it, the capabilities fall back to their own tools.

moirai and dftracer-utils are never imported here: capabilities run them through the Python in `MOIRAI_AGENTS_MOIRAI_PYTHON`.

## Tests

```bash
python3 -m pytest tests -q
MOIRAI_AGENTS_MOIRAI_PYTHON=<venv>/bin/python python3 tests/live_moirai.py <scratch dir>   # against a real moirai
```

## License

moirai-agents is distributed under the terms of the MIT license.
All new contributions must be made under this license.

See [LICENSE](LICENSE) and [NOTICE](NOTICE) for details.

SPDX-License-Identifier: MIT

LLNL-CODE-2024514 — Applied Storage Research (ASR)
