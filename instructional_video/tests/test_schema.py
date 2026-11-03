import copy

from ivideo.schema import SCRIPT_SCHEMA, assign_scene_ids, validate_script


def _strict_ok(schema: dict) -> None:
    """OpenRouter strict mode: every object lists all properties as required, no extras."""
    if schema.get("type") == "object":
        assert schema.get("additionalProperties") is False
        assert set(schema["required"]) == set(schema["properties"])
        for sub in schema["properties"].values():
            _strict_ok(sub)
    if schema.get("type") == "array":
        _strict_ok(schema["items"])


def test_schema_is_strict_compatible():
    _strict_ok(SCRIPT_SCHEMA)


def test_fixture_is_valid(demo_script):
    assert validate_script(demo_script, speakers=2) == []


def test_detects_problems(demo_script):
    bad = copy.deepcopy(demo_script)
    lines = bad["scenes"][0]["lines"]
    lines[0]["text"] = "**Bold** words"
    lines[1]["text"] = " ".join(["word"] * 41)
    lines[2]["speaker"] = "ghost"
    bad["scenes"][1]["lines"][0]["text"] = "(laughs) Okay then."
    errs = "\n".join(validate_script(bad, speakers=3))
    assert "markup" in errs
    assert "41 words" in errs
    assert "'ghost' is not a declared speaker" in errs
    assert "stage direction" in errs
    assert "expected 3 speakers" in errs


def test_unused_speaker(demo_script):
    bad = copy.deepcopy(demo_script)
    bad["speakers"].append({"id": "extra", "name": "X", "role": "learner"})
    assert any("never used" in e for e in validate_script(bad))


def test_scene_ids(demo_script):
    ids = [s["id"] for s in assign_scene_ids(demo_script)["scenes"]]
    assert ids == ["s01", "s02"]
