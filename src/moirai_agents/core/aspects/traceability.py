"""Traceability: where did this come from. The workflow and app instance, the processes and functions (activities) that made each
entity, the code region, the parameters it was run with and the attributes of what it made: the evidence engine's graph operations
(entities, activities, lineage, ends, group, inputs, outputs, attributes), owned here as an aspect."""
from __future__ import annotations

ASPECT, LABEL = "traceability", "Traceability"
SCOPE = ("Where did this come from: the workflow instance and app instance, the process and the function or code region that made it, "
         "the parameters (inputs) it was run with, what it returned (outputs), and the attributes of the entities, followed back to the "
         "roots (the inputs nothing in the traces produced) or forward to everything they fed.")
CORE_OPS = ("entities", "activities", "lineage", "ends", "group", "inputs", "outputs", "attributes")
CAPTURE = ("every function that reads or writes a scientific artifact as an activity with used/generated entities, its parameters as "
           "capture.inputs, its results as capture.outputs, the facts of each entity as attributes, and the workflow/app instance as "
           "entities (prov_annotate spec).")
OPS: dict = {}
OPWORD: dict = {}
