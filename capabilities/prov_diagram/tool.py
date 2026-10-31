"""Diagrams for the web UI: publish a graph or a Bokeh chart as a pipeline output, and check that what is shown matches its source."""
from moirai_agents.core import diagrams as D
from moirai_agents.core import pipeline as P

_SPEC = {
    "scientific_artifacts": ["out"],
    "entity_types": [{"type": "raw", "role": "input"}, {"type": "mid", "role": "intermediate"}, {"type": "out", "role": "output"}],
    "activities": [
        {"file": "m.py", "name": "merge", "function": "merge", "used": [{"type": "raw"}], "generated": [{"type": "mid", "when": "ok"}]},
        {"file": "m.py", "name": "save", "mode": "patch", "target": "S.go", "used": [{"type": "mid"}], "generated": [{"type": "out"}],
         "invalidated": [{"type": "mid"}], "relations": [{"relation": "contains", "subject": {"type": "out"}, "object": {"type": "mid"}}]},
    ],
}


def _figure(kind, title, x, y, xlabel, ylabel):
    """A Bokeh figure with pan, zoom, hover, reset and save; the SVG backend lets Save write a vector file."""
    from bokeh.models import ColumnDataSource, HoverTool
    from bokeh.palettes import Category10
    from bokeh.plotting import figure
    series = y if isinstance(y, dict) else {ylabel or "value": y}
    names = list(series)
    cols = Category10[10] if len(names) <= 10 else Category10[10] * (len(names) // 10 + 1)
    tools = "pan,wheel_zoom,box_zoom,reset,save"
    if kind in ("bar", "hbar"):
        cats = [str(v) for v in x]
        p = figure(title=title, x_range=cats, tools=tools, height=360, output_backend="svg", x_axis_label=xlabel, y_axis_label=ylabel) if kind == "bar" else \
            figure(title=title, y_range=list(reversed(cats)), tools=tools, height=max(240, 26 * len(cats) + 90), output_backend="svg", x_axis_label=ylabel, y_axis_label=xlabel)
        src = ColumnDataSource({"x": cats, **{n: series[n] for n in names}})
        if kind == "bar":
            p.vbar_stack(names, x="x", width=0.8, color=cols[:len(names)], source=src, legend_label=names) if len(names) > 1 else p.vbar(x="x", top=names[0], width=0.8, color=cols[0], source=src)
            p.xaxis.major_label_orientation = 0.8
        else:
            p.hbar_stack(names, y="x", height=0.8, color=cols[:len(names)], source=src, legend_label=names) if len(names) > 1 else p.hbar(y="x", right=names[0], height=0.8, color=cols[0], source=src)
        p.add_tools(HoverTool(tooltips=[("", "@x")] + [(n, f"@{{{n}}}") for n in names]))
    else:
        p = figure(title=title, tools=tools, height=360, output_backend="svg", x_axis_label=xlabel, y_axis_label=ylabel)
        for n, c in zip(names, cols):
            src = ColumnDataSource({"x": list(x), "y": series[n]})
            if kind == "line":
                p.line("x", "y", source=src, color=c, line_width=2, legend_label=n)
            p.scatter("x", "y", source=src, color=c, size=7, legend_label=n)
        p.add_tools(HoverTool(tooltips=[("x", "@x"), ("y", "@y")]))
    if p.legend:
        p.legend.click_policy = "hide"
        p.legend.location = "top_left"
    p.toolbar.logo = None
    return p


def _chart_problems(kind, x, y):
    out = []
    if kind not in ("bar", "hbar", "line", "scatter"):
        out.append(("kind", f"kind '{kind}'", "use bar, hbar, line or scatter"))
    series = y if isinstance(y, dict) else {"value": y}
    if not isinstance(x, list) or not x:
        out.append(("x", "x must be a non-empty list", "give the x values"))
    for n, v in series.items():
        if not isinstance(v, list) or (isinstance(x, list) and len(v) != len(x)):
            out.append(("lengths", f"series '{n}' has {len(v) if isinstance(v, list) else '?'} values for {len(x) if isinstance(x, list) else '?'} x values", "one y value per x value"))
    if kind in ("line", "scatter") and isinstance(x, list) and any(not isinstance(v, (int, float)) for v in x):
        out.append(("numeric_x", "line and scatter need numeric x", "use bar for categories"))
    return out


def register(mcp):
    @mcp.tool()
    def diagram_publish_graph(pipeline: str, stage: str, name: str, title: str, graph: dict, summary: str = "", source: str = "") -> dict:
        """Show a graph in the web UI as an interactive, editable Cytoscape diagram that can be downloaded as SVG, PNG, PDF or JSON. Use for any entity, lineage or flow
        picture a user must look at before approving a stage. pipeline, stage: where it is shown (a link appears on that stage in Steps, and on its gate). name: short file-safe
        id (a diagram of the same name is replaced). graph: {nodes: [{id, label, parent?, kind?, info?}], edges: [{source, target, label?, kind?, conditional?}], legend?}; parent
        groups nodes into a box; kind picks the colour (artifact, input, intermediate, output, parameter, reference, network, activity, file); info is shown when a node is
        selected. Returns {status, entry} or raises with every fault named."""
        return {"status": "ok", "entry": D.publish_graph(pipeline, stage, name, title, graph, summary, source)}

    @mcp.tool()
    def diagram_publish_annotation(pipeline: str, name: str = "annotation", title: str = "") -> dict:
        """Show what the annotation covers: build the entity-and-activity graph from the applied spec (<provenance>/spec.json) and publish it on the annotate stage. Use at the
        annotate gate, after annotate_apply and annotate_validate, and again whenever the spec changes. Every count comes from the spec. Returns {status, entry, counts}."""
        c = P.load(pipeline)
        import json
        try:
            spec = json.loads((P.work(c) / "spec.json").read_text())
        except (OSError, ValueError) as e:
            return {"status": "error", "error": f"no readable spec at {P.work(c) / 'spec.json'}: {e}", "fix": "run annotate_spec first"}
        g = D.from_spec(spec)
        by = {}
        for e in g["edges"]:
            by[e["kind"]] = by.get(e["kind"], 0) + 1
        counts = {"entity_types": len(spec.get("entity_types", [])), "activities": len(spec.get("activities", [])), "edges": len(g["edges"]), "by_kind": by,
                  "conditional": sum(1 for e in g["edges"] if e["conditional"]), "artifacts": len(spec.get("scientific_artifacts", []))}
        summary = (f"{counts['entity_types']} entity types, {counts['activities']} activities, {counts['edges']} edges "
                   f"({', '.join(f'{v} {k}' for k, v in sorted(by.items()))}); {counts['conditional']} conditional; {counts['artifacts']} scientific artifacts")
        entry = D.publish_graph(pipeline, "annotate", name, title or "What the annotation covers", g, summary, str(P.work(c) / "spec.json"))
        return {"status": "ok", "entry": entry, "counts": counts}

    @mcp.tool()
    def diagram_publish_chart(pipeline: str, stage: str, name: str, title: str, kind: str, x: list, y: dict, xlabel: str = "", ylabel: str = "", summary: str = "") -> dict:
        """Show an interactive Bokeh chart in the web UI (pan, zoom, hover, hide a series by clicking its legend, download as SVG or PDF). Use for numbers, not for graphs:
        timings per stage, counts per type, a metric over runs. kind: bar | hbar | line | scatter. x: the categories (bar, hbar) or numbers (line, scatter). y: {series name: one
        number per x}. stage: where it is shown. Returns {status, entry} or {status: error, problems}."""
        bad = _chart_problems(kind, x, y)
        if bad:
            return {"status": "error", "problems": [{"check": a, "detail": b, "fix": c} for a, b, c in bad]}
        from bokeh.embed import json_item
        item = json_item(_figure(kind, title, x, y, xlabel, ylabel), name)
        return {"status": "ok", "entry": D.publish_bokeh(pipeline, stage, name, title, item, summary, "")}

    @mcp.tool()
    def diagram_list(pipeline: str, stage: str = "") -> dict:
        """List the diagrams a pipeline (or one stage) shows in the web UI, with their counts and whether the user edited them. Returns {status, diagrams: [entry]}."""
        return {"status": "ok", "diagrams": D.listing(P.load(pipeline), stage or None)}

    @mcp.tool()
    def diagram_check(pipeline: str, name: str) -> dict:
        """Rule on one published diagram. Use after publishing, and before showing it at a gate. Checks: it is indexed and its file reads; the graph (or the Bokeh item) has no
        fault; the stage records it as an output; an annotation diagram still matches the saved spec (same entity types, activities and edges), and the user's edited copy, if any,
        is valid. Returns the verdict {status, failed, unknown, passed, total}; each failure names the fix."""
        c = P.load(pipeline)
        v = P.Verdict("diagram")
        try:
            d = D.read(c, name)
        except P.PipelineError as e:
            v.check("published", False, str(e), "publish it with diagram_publish_graph, diagram_publish_annotation or diagram_publish_chart")
            return v.result()
        v.check("published", True)
        bad = D.bokeh_problems(d["item"]) if d["kind"] == "bokeh" else D.graph_problems(d["graph"])
        v.check("content", not bad, "; ".join(f"{a}: {b}" for a, b, _ in bad)[:400], "; ".join(f for _, _, f in bad)[:300])
        v.check("recorded", name in (P.stage(c, d["stage"])["outputs"].get("diagrams") or []), f"stage {d['stage']} does not list {name} in its outputs", "publish it again")
        if d["kind"] == "graph" and name == "annotation":
            import json
            try:
                spec = json.loads((P.work(c) / "spec.json").read_text())
            except (OSError, ValueError):
                v.assume("matches_spec", False, "no spec.json to compare with")
            else:
                now = D.from_spec(spec)
                same = ({n["id"] for n in now["nodes"]} == {n["id"] for n in d["graph"]["nodes"]}
                        and sorted((e["source"], e["target"], e["kind"], e["conditional"]) for e in now["edges"]) == sorted((e["source"], e["target"], e["kind"], e["conditional"]) for e in d["graph"]["edges"]))
                v.check("matches_spec", same, f"the diagram has {len(d['graph']['nodes'])} nodes / {len(d['graph']['edges'])} edges, the spec gives {len(now['nodes'])} / {len(now['edges'])}",
                        "the spec changed after the diagram was published: run diagram_publish_annotation again")
        if d.get("edited"):
            ebad = D.graph_problems(d["edited"])
            v.check("edited_valid", not ebad, "; ".join(f"{a}: {b}" for a, b, _ in ebad)[:300], "reset the edit in the page, or fix the edited graph")
        return v.result()

    @mcp.tool()
    def diagram_selftest() -> dict:
        """Reproduce the diagram logic on a small spec this tool carries: the graph built from a spec (a patch, a conditional edge, an invalidation, an entity relation), the
        faults a bad graph reports, the diff of an edited graph, and a Bokeh chart. Use after a change to the diagram code. Changes nothing. Returns {status, line}."""
        g = D.from_spec(_SPEC)
        by = {}
        for e in g["edges"]:
            by[e["kind"]] = by.get(e["kind"], 0) + 1
        import copy
        edited = copy.deepcopy({"nodes": [n for n in g["nodes"] if n["id"] != "t:raw"] + [{"id": "x", "label": "extra"}],
                                "edges": [e for e in g["edges"] if "t:raw" not in (e["source"], e["target"])] + [{"source": "x", "target": "a:merge", "kind": "flow"}]})
        for n in edited["nodes"]:
            if n["id"] == "a:merge":
                n["label"] = "merge2"
        ch = D.diff(g, edited)
        broken = {"nodes": [{"id": "a"}, {"id": "a"}], "edges": [{"source": "a", "target": "zz"}]}
        try:
            fig = _figure("bar", "t", ["a", "b"], {"n": [1, 2]}, "", "")
            from bokeh.embed import json_item
            chart = "ok" if not D.bokeh_problems(json_item(fig, "t")) else "bad"
        except Exception as ex:      # the line says what failed, the check compares it
            chart = f"error:{type(ex).__name__}"
        line = (f"nodes={len(g['nodes'])} edges={len(g['edges'])} conditional={sum(1 for e in g['edges'] if e['conditional'])} kinds=" + ",".join(f"{k}:{by[k]}" for k in sorted(by))
                + f" artifacts={sum(1 for n in g['nodes'] if n['kind'] == 'artifact')} problems={len(D.graph_problems(g))} bad_graph_faults={len(D.graph_problems(broken))}"
                + f" diff=-n{len(ch['removed_nodes'])}/+n{len(ch['added_nodes'])}/-e{len(ch['removed_edges'])}/+e{len(ch['added_edges'])}/r{len(ch['renamed'])} chart={chart}")
        return {"status": "ok", "line": line}
