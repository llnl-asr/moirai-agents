"""The inputs of an app's live runs - what a scientist tunes when starting one - detected and written in the annotation phase,
when the app has been explored and annotated. The Live Run view shows them as dropdowns and fields. See core.liveinputs."""
from moirai_agents.core import liveinputs as LI
from moirai_agents.core import pipeline as P


def register(mcp):
    @mcp.tool()
    def live_inputs_detect(pipeline: str) -> dict:
        """Find candidate live-run inputs of an annotated app: argparse arguments of its Python code (choices, defaults, types), parameters the
        annotation reads (resolve(kwargs, 'x', env, 'X')) and files the run command names (their siblings as choices). Use in the annotation phase,
        once the app is explored and annotated (at the latest before package). pipeline: the run pipeline (or a live run: its app is used).
        Returns {source, command, candidates: [{name, kind, options?, default?, flag?, env?, help?, source}], next}."""
        return LI.detect(P.load(pipeline))

    @mcp.tool()
    def live_inputs_write(pipeline: str, command: str, inputs: list, notes: str = "") -> dict:
        """Keep the live-run inputs of an app, after curating live_inputs_detect's candidates with what you learned exploring the app: what a scientist
        changes between long runs (the input data, modes, seeds, sizes, the queue), never internals. command: the real-run command template with a
        {name} slot per input, e.g. "flux batch -N {nodes} run.sh --config {config} --dock-mode {dock_mode}". inputs: [{name, label, kind: choice|integer|
        number|text|path|bool, options (choice), default, min, max (numbers), help, required}]. Every slot is an input and every input is used.
        Returns {status, inputs, example: the command with the defaults} or {status: error, problems}."""
        try:
            return LI.write(pipeline, {"command": command, "inputs": inputs, "notes": notes})
        except P.PipelineError as e:
            return {"status": "error", "problems": [str(e)]}

    @mcp.tool()
    def live_inputs_render(pipeline: str, values: dict) -> dict:
        """The exact live-run command for chosen values (missing ones take their default), each checked against its input and shell-quoted.
        pipeline: the run pipeline or one of its live runs. Use before moirai_live_run to show the user the command. Returns {status, command, values,
        warnings} or {status: error, problems}."""
        c = P.load(pipeline)
        spec = LI.get(c)
        if not spec:
            return {"status": "error", "problems": ["this app has no live-run inputs yet: live_inputs_detect, then live_inputs_write, on its run pipeline"]}
        r = LI.render(spec, values or {})
        if r["status"] == "ok":
            r["warnings"] = LI.path_warnings(spec, values or {}, P.source_of(P.origin(c)))
        return r

    @mcp.tool()
    def live_inputs_validate(pipeline: str) -> dict:
        """Validator: the app has live-run inputs, the spec is consistent (every slot an input, every input used, choices with options, defaults valid)
        and the defaults render a command. Use after live_inputs_write. Returns the verdict {status, failed, unknown, passed, total}."""
        c = P.load(pipeline)
        spec = LI.get(c)
        v = P.Verdict("live_inputs")
        if v.check("written", bool(spec), "no live-run inputs", "live_inputs_detect, then live_inputs_write"):
            bad = LI.check(spec)
            v.check("consistent", not bad, "; ".join(bad)[:400], "fix the spec and live_inputs_write again")
            if not bad:
                r = LI.render(spec, {})
                v.check("defaults_render", r["status"] == "ok", "; ".join(r.get("problems", []))[:400], "give every required input a valid default")
        return v.result()
