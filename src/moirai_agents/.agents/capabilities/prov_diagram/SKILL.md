---
name: prov_diagram
description: Diagrams in the web UI as pipeline outputs. An interactive, editable Cytoscape graph (zoom, drag, rename, add or remove nodes and edges, download SVG, PNG, PDF or JSON) of what an annotation covers or any lineage, and interactive Bokeh charts for numbers. Load at the annotate gate to give the user the picture to approve, when a stage's result is easier to see than to read, or when a user asks for a diagram, graph, chart or figure in the UI.
---

Cross-references: [[prov_annotate]] [[dftracer-provenance]] [[workload_elmerfold]] [[prov_dashboard]]

# Diagrams in the web UI

## Rules
- At the annotate gate, after `annotate_apply` and a passing `annotate_validate`: `diagram_publish_annotation`, then `diagram_check`. The user finds the diagram on the gate card and, afterwards, as the "diagram" link of the annotate row in Steps. It is a pipeline output: it stays after the gate for later reference, and the repro package should carry `<provenance>/diagrams/`.
- Graphs (entity types, activities, lineage, flows) go through `diagram_publish_graph`; numbers (timings, counts, a metric over runs) go through `diagram_publish_chart`. Do not draw a chart as a graph or the reverse.
- The picture must answer a question the user has to decide. Name it in the title and the summary ("Is every dataset you care about a box?"). One diagram per decision; split rather than cram.
- Every number, node and edge comes from a spec, a trace or a tool result, never from memory. `diagram_publish_annotation` builds the graph from `<provenance>/spec.json`; run it again whenever the spec changes (`diagram_check` fails `matches_spec` otherwise).
- The user may edit a graph in the page (rename, add or remove nodes and edges, move them) and save the edit. The published graph is never overwritten; the edit is kept beside it and shown with the changes. Treat a saved edit as feedback: read `diagram_list` (`edited_ts`) and the changes, and ask what they mean for the spec before acting.

## Constraints
- Labels are plain text of at most 240 characters; put detail in a node's `info`. No HTML.
- At most 800 nodes and 4000 edges per graph; collapse repeated nodes (for example per-model ids) into one with a note.
- Bokeh charts are built by the tool in the server's Python: the page loads BokehJS from the same installed package. If `bokeh` is missing the chart tool says so and the graph tools still work.
- It never edits annotated code or the spec.

## Corner cases
- Many edges between the same two nodes with the same kind and condition are drawn once.
- A node with `parent` is drawn inside that node (activities inside their source file).
- Publishing under an existing name replaces the diagram and drops the earlier user edit.
- Browser-side download is SVG (vector, written by the page), PNG, JSON and PDF (vector, from the SVG). The server makes no PDF.
- A conditional edge (it exists only if a condition holds) is dashed; invalidated is red and dotted.

## Corrector
- `published` failed: publish it with the right tool; names are file-safe (letters, digits, `.` `_` `-`).
- `content` failed: read each named fault; fix the graph (unique node ids, edge ends that exist, no parent cycle, labels short) or rebuild the Bokeh item with the installed bokeh.
- `recorded` failed: publish again; the stage outputs list the diagram names.
- `matches_spec` failed: the spec changed after publishing; run `diagram_publish_annotation` again and show the user the new picture.
- `edited_valid` failed: the user's saved edit is broken; ask them to reset it in the page.

## Self-heal
- The page says it could not load a library: check `static/vendor/THIRD_PARTY.md` against the files, and `/vendor/bokeh/bokeh.min.js` for charts.
- A graph that looks wrong but passes `diagram_check` is a spec problem, not a drawing problem: fix the spec with `prov_annotate`.
- If the page cannot draw a kind of node or edge, add it to the `kind` colours in `diagrams.js` and a case to `diagram_selftest`, then `capability_update`.

## Self-learn
- A picture the user asked for again and again becomes a tool here (`diagram_publish_<x>`), with a check, instead of a one-off.
- Anything new explored here becomes a capability, constantly: a new workload, software, tool or system met while drawing is captured before moving on.
