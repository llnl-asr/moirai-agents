# moirai-agents

Capability-centric agents for [moirai](https://github.com/llnl-asr/moirai) data provenance: annotate an application, run it, watch its provenance live, and explore it, with a person approving every step. It builds on [dftracer-agents](https://github.com/llnl-asr/dftracer-agents) and follows the capability model of Phronix.

## Everything is a capability

A capability is one folder in `src/moirai_agents/.agents/capabilities/<name>/`. The kernel has no tools of its own.

| File | Holds |
|---|---|
| `tool.py` | the MCP tools: predictors, and the validators that rule on what they produced |
| `SKILL.md` | Rules, Constraints, Corner cases, Corrector, Self-heal, Self-learn |
| `agent.json` | the background agent: its fixed scope of tools, skills, permissions and level |
| `checks.json` | known input to expected output, run in a sandbox on every change |
| `capability.json` | version, stages, history, and `pvc`: which tools predict and which validate |
| `LESSONS.md` | what use has taught it |

Every capability is a **predictor, validator, corrector** loop. A tool predicts. A separate deterministic tool validates. On failure, the verdict sends the agent to the skill's Corrector section, which repairs the original tool or its input; then it predicts and validates again. Attempts follow measured progress: three to start, more while the failures change, and the same failure twice stops the loop for the user. A bundle that does not declare the loop is refused.

`capability_builder` is the capability that builds capabilities: `capability_new` and `capability_update` validate a proposal and run its checks in a sandbox, a person approves, and it goes live with no restart.

Layers, lowest first: bundled, user (`~/.moirai-agents/capabilities`), project (`capabilities/` in this repo). A higher layer shadows a lower one wholesale, and the bundled layer is never written.

## The pipelines

Each stage has one owning capability and stops at a user gate.

| Pipeline | Stages |
|---|---|
| run | explore, smoke, annotate, integrate, moirai_smoke, baseline, query |
| analysis | ingest, dashboard, query |

At a gate the user can approve, give **process** feedback (becomes a new or updated capability, which they approve) or say **remember** (becomes a lesson of that capability at once). A stage passes only with a passing verdict of its current outputs, an approval, and all feedback resolved.

## Web UI

```bash
scripts/setup_env.sh          # .venv, with dftracer-agents editable
$EDITOR .env                  # MOIRAI_AGENTS_MOIRAI_PYTHON, model access
scripts/webui_start.sh        # prints your link and the tunnel command
scripts/webui_stop.sh         # stops it and everything it started
```

You run it yourself, as yourself: there are no accounts and no sign-in. Open the printed link; its private token only keeps other people on a shared login node out of an agent running as you.

Chats on the left, Chat and Dashboard in the middle, and Steps, Agents, Jobs, Lessons and Capabilities on the right. Models: Claude, OpenAI or LivAI, each verified before it is saved. Every chat runs Claude Code in your workspace.

**The Dashboard is part of the web server**: one process, one port. A running `moirai-service` already produces the trace, so the server tails it and pushes new events to the page. The trace is a sequence of independent gzip members and the service seals one at least every `MOIRAI_PC_FLUSH_MS`; the tail commits only whole members, so an unfinished member or a gap is simply read next time. Finished traces take the same path. Its view is the provenance graph of dftracer-utils' viewer, simplified: pick entities grouped by type, and show or hide activities, each entity type and I/O. I/O is attributed to activities as the server does, in streaming form.

**It refreshes itself.** A capability's `tool.py` reloads in the MCP server, which tells the client. A change to the kernel re-executes the MCP server in place, with its connection intact. In the web UI, `api.py`, `prompts.py`, `core/` and static files reload during a chat, and open pages follow. `runtime.py`, `dashboard.py` and `server.py` re-execute on the same socket once no chat is running, and logins survive. An edit that does not compile is refused and shown on the page; the running code stays.

**It does not outlive its use.** After `MOIRAI_AGENTS_WEBUI_IDLE_S` (2 h) with no user action, chat or job, and in any case after `MOIRAI_AGENTS_WEBUI_MAX_S` (12 h), it stops its chats, sweeps every process it started and exits. Page polling does not count as activity. Background jobs have their own limit (`limit`, default `MOIRAI_AGENTS_JOB_LIMIT_S`, 1 h); on expiry they get SIGTERM, so their traps stop `moirai-service`.

## dftracer-agents

`scripts/setup_env.sh` installs it editable. The workspace uses its skills and agents, its permission policy, and its MCP server beside ours. Sessions and pipelines both live under `<workspace>/workspaces/`, the path that policy allows. `smoke_session` runs dftracer's own session tools, and `df_call` reaches any other. Without it, the capabilities fall back to their own tools.

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
