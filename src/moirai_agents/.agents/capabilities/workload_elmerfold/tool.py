"""Elmerfold (OpenFold monomer structure prediction) provenance annotation facts that have one right answer."""
import json

TYPE_LIMIT = 31

BATCH = {
    "functions": {
        "load_and_merge_msas": {"capture": {"outputs": {"sequence_length": "<sequence length>", "merged_msa_bytes": "<merged msa size in bytes>"}}},
        "template_featgen": {
            "attributes": ["num_templates"],
            "outputs": ["num_templates"],
            "note": "num_templates comes from the return value of generate_template_features",
        },
        "load_inputs": {"capture": {"inputs": {"input_chunk_file": "<path of the input chunk file>"}}},
    },
    "activities": {
        "subprocess_finish": {
            "mode": "patch",
            "target": "SubprocessTask.on_finish (ems/src/ems/task.py base hook)",
            "generates": "pipeline_task",
            "attributes": ["returncode", "error"],
            "answers": "which sequences failed the template/filter step",
        },
        "stage_setup": {
            "mode": "statement",
            "after": "self._stage_dir = stage_dir in OF2StructurePredictionPipeline.__init__",
            "generates": "stage_area",
            "attributes": ["mount_point", "mount_device", "mount_fstype"],
            "attribute_source": "/proc/mounts entry covering stage_dir",
            "inputs": ["user", "host"],
            "note": "identifies the Rabbit mount",
        },
    },
}


def _flat(spec) -> str:
    return json.dumps(spec, sort_keys=True)


def register(mcp):
    @mcp.tool()
    def elmerfold_gap_batch() -> dict:
        """The Elmerfold 23-gap batch spec changes to apply to the annotation spec: function capture/outputs/attributes and the two new
        activities (subprocess_finish, stage_setup). Use before annotate_spec for an elmerfold app. The spec is always sent whole to
        annotate_spec, never as a patch. Returns {batch, notes}."""
        return {"batch": BATCH, "notes": [
            "send the whole spec to annotate_spec every time",
            "types are cut at 31 chars in traces: keep entity/activity type names unique within 31 chars",
            "gen_launch_script.py derives --nodes as chunks + 1 (one launch node)"]}

    @mcp.tool()
    def elmerfold_nodes(chunks: int) -> dict:
        """Nodes gen_launch_script.py requests for a number of input chunks: chunks + 1 (the extra one is the drained launch node).
        chunks: number of input chunks (>=1). Returns {chunks, nodes}."""
        if chunks < 1:
            raise ValueError("chunks must be >= 1")
        return {"chunks": chunks, "nodes": chunks + 1}

    @mcp.tool()
    def elmerfold_type_cut(name: str) -> dict:
        """How a type name appears in traces: cut at 31 characters. Use to check two types stay distinct in a query.
        name: entity or activity type. Returns {name, in_trace, cut}."""
        return {"name": name, "in_trace": name[:TYPE_LIMIT], "cut": len(name) > TYPE_LIMIT}

    @mcp.tool()
    def elmerfold_spec_validate(spec: str) -> dict:
        """Validator: a whole annotation spec (JSON text) carries every gap-batch item. Names the missing ones.
        spec: JSON text of the spec. Returns {ok, missing, too_long_types}."""
        s = json.loads(spec)
        text = _flat(s)
        need = ["load_and_merge_msas", "template_featgen", "num_templates", "subprocess_finish", "on_finish", "pipeline_task",
                "returncode", "stage_setup", "stage_area", "mount_point", "mount_device", "mount_fstype", "load_inputs"]
        missing = [n for n in need if n not in text]
        types = []

        def walk(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    if k in ("type", "entity_type", "generates") and isinstance(v, str):
                        types.append(v)
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
        walk(s)
        long = sorted({t for t in types if len(t) > TYPE_LIMIT})
        return {"ok": not missing and not long, "missing": missing, "too_long_types": long,
                "fix": "add the missing items from elmerfold_gap_batch; shorten types to 31 chars" if missing or long else ""}
