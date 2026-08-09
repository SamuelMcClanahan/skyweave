"""Budget runner: determinism (G10) and report content."""

from __future__ import annotations

from dataclasses import replace

from skyweave2.geometry import GeometryConfig
from skyweave2.geometry.budget import BudgetSpec, generate_report, main


def test_g10_budget_is_byte_identical_for_fixed_seed(tmp_path):
    """G10: two runs with the same seed produce identical bytes."""
    out_a = tmp_path / "budget_a.md"
    out_b = tmp_path / "budget_b.md"
    main(["--seed", "7", "--out", str(out_a), "--trials", "5"])
    main(["--seed", "7", "--out", str(out_b), "--trials", "5"])
    assert out_a.read_bytes() == out_b.read_bytes()
    assert out_a.read_bytes() != b""


def test_budget_report_answers_the_five_questions():
    spec = replace(BudgetSpec(), trials=5)
    report = generate_report(seed=3, trials=5, spec=spec, config=GeometryConfig())
    assert "Modeled" in report
    assert "Measured" not in report.replace("Nothing here is Measured", "")
    for heading in ("## Q1", "## Q2", "## Q3", "## Q4", "## Q5"):
        assert heading in report
    assert "Table A" in report and "Table B" in report


def test_different_seeds_differ():
    spec = replace(BudgetSpec(), trials=5)
    a = generate_report(seed=1, trials=5, spec=spec, config=GeometryConfig())
    b = generate_report(seed=2, trials=5, spec=spec, config=GeometryConfig())
    assert a != b
