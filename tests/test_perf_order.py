import ast

from moirai_agents.core.perf_order import use_moirai

SRC = "from __future__ import annotations\nimport os\nfrom dftracer.python import dftracer, dft_fn as DFTracerFn\n_dft = DFTracerFn('x')\n"


def test_the_dftracer_import_becomes_a_moirai_import():
    out = use_moirai(SRC)
    assert "from moirai import dftracer, dft_fn as DFTracerFn" in out.split("\n")
    assert "dftracer.python" not in out
    ast.parse(out)


def test_idempotent_and_untouched_cases():
    once = use_moirai(SRC)
    assert use_moirai(once) == once
    assert use_moirai("import os\n") == "import os\n"
    assert use_moirai("    from dftracer.python import x\n") == "    from dftracer.python import x\n"      # only top-level imports


def test_the_earlier_ordering_line_is_dropped():
    old = "import _moirai_prov  # noqa: F401  moirai sets DFTRACER_* before dftracer is imported\nfrom dftracer.python import dftracer\n"
    assert use_moirai(old) == "from moirai import dftracer\n"
