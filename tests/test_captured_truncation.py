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
