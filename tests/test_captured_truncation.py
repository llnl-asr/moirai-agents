"""The trace keeps 31 characters of an entity type: a captured type longer than that comes back cut ("...attribut")."""
from moirai_agents.core import evidence
from moirai_agents.core.aggregate import captured_type, ends


def test_cut_attribute_type_is_captured():
    assert len("of3_template_alignment.attribut") == 31
    assert captured_type("of3_template_alignment.attribut")
    assert captured_type("t:of3_template_alignment.attribut")
    assert evidence.is_captured("of3_template_alignment.attribut")
    assert ends("of3_template_alignment.attribut", ".attribute")
    assert not ends("of3_template_alignment.attribut", ".input")


def test_normal_types_are_not_captured():
    assert captured_type("best_structure.attribute") and captured_type("filter_msa.input")
    assert not captured_type("of3_template_alignment") and not captured_type("sequence")
    assert not ends("abc.attribution_plan_of_something_long", ".attribute")


def test_synthesis_must_open_with_the_answer():
    from moirai_agents.core.evidence import check_synthesis
    computed = "10 entities 5 runs"
    ok = check_synthesis("The traces show 10 entities in 5 runs [S1].\n\n- evidence [S1]", 1, computed)
    assert ok["ok"], ok
    for bad in ("| a | b |\n|---|---|\n| 1 | 2 | [S1]", "# Result\nThe traces show 10 entities [S1]", "- 10 entities [S1]", "10 [S1]"):
        r = check_synthesis(bad, 1, computed)
        assert not r["ok"] and any("answer" in p or "opens" in p for p in r["problems"]), (bad, r)
