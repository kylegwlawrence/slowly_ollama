"""Phase 23: tests for the degree-outline schema.

Each rejection test starts from a known-valid outline (built by
``_valid_outline``) and mutates one field — that keeps the diff between the
"good" and "bad" cases small and the assertion obvious.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.degree_schema import (
    ACTION_VERBS,
    Outline,
    validate_outline,
)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _verb_outcomes(n: int) -> list[str]:
    """Return ``n`` outcome phrases, each starting with an allowed verb.

    Cycles through ACTION_VERBS so the phrases aren't all identical (useful
    for tests that lowercase-dedupe inside a unit).
    """
    verbs = sorted(ACTION_VERBS)
    return [f"{verbs[i % len(verbs)]} concept_{i} under condition_{i}" for i in range(n)]


def _terms(prefix: str, n: int) -> list[str]:
    """Return ``n`` unique pseudo-terms keyed by prefix (avoid cross-unit clash)."""
    return [f"{prefix}_term_{i}" for i in range(n)]


def _standard_unit(slug: str, n: int, weeks_start: int) -> dict:
    """Build one standard 3-week unit at ``weeks_start``."""
    return {
        "n": n,
        "slug": slug,
        "title": f"Unit {n}",
        "focus": "one-line focus",
        "weeks_start": weeks_start,
        "weeks_end": weeks_start + 2,  # 3 weeks
        "is_capstone": False,
        "outcome_phrases": _verb_outcomes(6),
        "key_concepts": [f"concept_{i}" for i in range(8)],
        "glossary_terms": _terms(slug, 12),
        "weeks": [
            {
                "n": weeks_start,
                "slug": f"{slug}_w1",
                "title": "Week 1",
                "focus": "f1",
                "outcome_phrases": _verb_outcomes(3),
                "key_term_names": [f"kt_{i}" for i in range(6)],
            },
            {
                "n": weeks_start + 1,
                "slug": f"{slug}_w2",
                "title": "Week 2",
                "focus": "f2",
                "outcome_phrases": _verb_outcomes(3),
                "key_term_names": [f"kt_{i}" for i in range(6)],
            },
            {
                "n": weeks_start + 2,
                "slug": f"{slug}_w3",
                "title": "Week 3",
                "focus": "f3",
                "outcome_phrases": _verb_outcomes(4),
                "key_term_names": [f"kt_{i}" for i in range(8)],
            },
        ],
    }


def _capstone_unit(slug: str, n: int, weeks_start: int) -> dict:
    """Build one 3-week capstone unit (Proposal / Execution / Submission)."""
    return {
        "n": n,
        "slug": slug,
        "title": f"Capstone {n}",
        "focus": "capstone integration",
        "weeks_start": weeks_start,
        "weeks_end": weeks_start + 2,
        "is_capstone": True,
        "weeks": [
            {
                "n": weeks_start,
                "slug": "proposal",
                "title": "Proposal",
                "phase": "Proposal",
            },
            {
                "n": weeks_start + 1,
                "slug": "execution",
                "title": "Execution",
                "phase": "Execution",
            },
            {
                "n": weeks_start + 2,
                "slug": "submission",
                "title": "Submission",
                "phase": "Submission",
            },
        ],
    }


def _standard_course(slug: str, n: int, weeks_start: int) -> dict:
    """Build a 1-unit, 3-week standard course."""
    return {
        "n": n,
        "slug": slug,
        "title": f"Course {n}",
        "focus": "course focus",
        "tier": "intro",
        "weeks_start": weeks_start,
        "weeks_end": weeks_start + 2,
        "key_capability": "do the thing",
        "is_capstone": False,
        "units": [_standard_unit(f"{slug}_u1", 1, weeks_start)],
    }


def _capstone_course(slug: str, n: int, weeks_start: int) -> dict:
    return {
        "n": n,
        "slug": slug,
        "title": f"Course {n} (capstone)",
        "focus": "synthesize everything",
        "tier": "frontier",
        "weeks_start": weeks_start,
        "weeks_end": weeks_start + 2,
        "is_capstone": True,
        "units": [_capstone_unit("capstone_unit", 1, weeks_start)],
    }


def _valid_outline(total_courses: int = 4) -> dict:
    """Build a minimal valid outline with ``total_courses`` courses.

    Standard courses come first; the final course is always the capstone.
    Each course is 1 unit of 3 weeks → 3 weeks per course, dense across
    the degree.
    """
    courses: list[dict] = []
    week = 1
    for i in range(1, total_courses):
        courses.append(_standard_course(f"course{i}", i, week))
        week += 3
    courses.append(_capstone_course(f"course{total_courses}", total_courses, week))

    return {
        "version": 1,
        "degree": {
            "slug": "test_degree",
            "title": "Test Degree",
            "tier_reached": "frontier",
            "prerequisites": "calculus, time, willpower",
            "total_courses": total_courses,
            "themes": [
                "Theme A: a tension across courses",
                "Theme B: another tension",
                "Theme C: a third tension",
                "Theme D: a fourth tension",
            ],
            "program_outcome_phrases": _verb_outcomes(6),
        },
        "courses": courses,
    }


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_happy_path_4_courses_validates() -> None:
    outline = _valid_outline(4)
    result = validate_outline(outline)
    assert isinstance(result, Outline)
    assert result.degree.total_courses == 4
    assert result.courses[-1].is_capstone is True


def test_happy_path_5_courses_validates() -> None:
    validate_outline(_valid_outline(5))


def test_happy_path_6_courses_validates() -> None:
    validate_outline(_valid_outline(6))


def test_validate_outline_accepts_path(tmp_path: Path) -> None:
    """The Path overload reads and parses a file."""
    outline_path = tmp_path / "outline.json"
    outline_path.write_text(json.dumps(_valid_outline(4)))
    result = validate_outline(outline_path)
    assert isinstance(result, Outline)


def test_validate_outline_accepts_json_string() -> None:
    """The str overload parses JSON."""
    raw = json.dumps(_valid_outline(4))
    assert isinstance(validate_outline(raw), Outline)


# ---------------------------------------------------------------------------
# Degree-level rejections
# ---------------------------------------------------------------------------


def test_rejects_3_courses() -> None:
    """``total_courses`` < 4 is invalid (3 should be a single course)."""
    outline = _valid_outline(4)
    outline["degree"]["total_courses"] = 3
    outline["courses"] = outline["courses"][:3]
    # Make course 3 (last) the capstone since the previous capstone was at index 3.
    # Actually easier: just truncate and rebuild — but the field validator on
    # total_courses fires before the cross-validator runs, so the count check
    # alone is the rejection.
    with pytest.raises(ValidationError, match="greater than or equal to 4"):
        validate_outline(outline)


def test_rejects_7_courses() -> None:
    """``total_courses`` > 6 is invalid (split into two degrees)."""
    outline = _valid_outline(4)
    outline["degree"]["total_courses"] = 7
    with pytest.raises(ValidationError, match="less than or equal to 6"):
        validate_outline(outline)


def test_rejects_total_courses_mismatch() -> None:
    """When ``total_courses`` disagrees with len(courses), reject."""
    outline = _valid_outline(4)
    outline["degree"]["total_courses"] = 5
    with pytest.raises(ValidationError, match="total_courses=5"):
        validate_outline(outline)


def test_rejects_non_capstone_last_course() -> None:
    """The final course MUST be a capstone."""
    outline = _valid_outline(4)
    outline["courses"][-1]["is_capstone"] = False
    # Also unmark the capstone unit so we hit the right validator (otherwise
    # the course-level capstone-consistency check fires first).
    outline["courses"][-1]["units"][0]["is_capstone"] = False
    # And give the unit standard-unit anchors so its shape passes.
    outline["courses"][-1]["units"][0].update(
        outcome_phrases=_verb_outcomes(6),
        key_concepts=[f"c_{i}" for i in range(8)],
        glossary_terms=[f"caps_t_{i}" for i in range(12)],
    )
    # Make the weeks standard too.
    weeks_start = outline["courses"][-1]["weeks_start"]
    outline["courses"][-1]["units"][0]["weeks"] = [
        {
            "n": weeks_start,
            "slug": "w_a",
            "title": "wa",
            "focus": "fa",
            "outcome_phrases": _verb_outcomes(3),
            "key_term_names": [f"k_{i}" for i in range(6)],
        },
        {
            "n": weeks_start + 1,
            "slug": "w_b",
            "title": "wb",
            "focus": "fb",
            "outcome_phrases": _verb_outcomes(3),
            "key_term_names": [f"k_{i}" for i in range(6)],
        },
        {
            "n": weeks_start + 2,
            "slug": "w_c",
            "title": "wc",
            "focus": "fc",
            "outcome_phrases": _verb_outcomes(3),
            "key_term_names": [f"k_{i}" for i in range(6)],
        },
    ]
    with pytest.raises(ValidationError, match="must have is_capstone=True"):
        validate_outline(outline)


def test_rejects_capstone_on_non_final_course() -> None:
    """Only the final course may be capstone. To isolate the Outline-level
    rule from the Course-level capstone-consistency rule, mark the unit
    capstone too — that satisfies the inner check so the outer one fires."""
    outline = _valid_outline(4)
    outline["courses"][0]["is_capstone"] = True
    outline["courses"][0]["units"][0]["is_capstone"] = True
    # Make the unit valid as a capstone (no standard anchors, phase-bearing weeks).
    outline["courses"][0]["units"][0].update(
        outcome_phrases=None, key_concepts=None, glossary_terms=None,
    )
    weeks_start = outline["courses"][0]["units"][0]["weeks_start"]
    outline["courses"][0]["units"][0]["weeks"] = [
        {"n": weeks_start, "slug": "p", "title": "P", "phase": "Proposal"},
        {"n": weeks_start + 1, "slug": "e", "title": "E", "phase": "Execution"},
        {"n": weeks_start + 2, "slug": "s", "title": "S", "phase": "Submission"},
    ]
    with pytest.raises(ValidationError, match="only the final course may be capstone"):
        validate_outline(outline)


def test_rejects_duplicate_course_slugs() -> None:
    outline = _valid_outline(4)
    outline["courses"][1]["slug"] = outline["courses"][0]["slug"]
    with pytest.raises(ValidationError, match="duplicate course slugs"):
        validate_outline(outline)


def test_rejects_only_3_themes() -> None:
    outline = _valid_outline(4)
    outline["degree"]["themes"] = outline["degree"]["themes"][:3]
    with pytest.raises(ValidationError):
        validate_outline(outline)


def test_rejects_only_5_program_outcomes() -> None:
    outline = _valid_outline(4)
    outline["degree"]["program_outcome_phrases"] = (
        outline["degree"]["program_outcome_phrases"][:5]
    )
    with pytest.raises(ValidationError):
        validate_outline(outline)


def test_rejects_program_outcome_with_invalid_verb() -> None:
    outline = _valid_outline(4)
    outline["degree"]["program_outcome_phrases"][0] = "Understand quantum mechanics"
    with pytest.raises(ValidationError, match="action verb"):
        validate_outline(outline)


# ---------------------------------------------------------------------------
# Week-numbering rejections
# ---------------------------------------------------------------------------


def test_rejects_sparse_weeks_across_courses() -> None:
    """Course 2 must start at the week after Course 1 ends, not later."""
    outline = _valid_outline(4)
    # Push course 2 forward by one week — sparse boundary.
    outline["courses"][1]["weeks_start"] += 1
    outline["courses"][1]["weeks_end"] += 1
    outline["courses"][1]["units"][0]["weeks_start"] += 1
    outline["courses"][1]["units"][0]["weeks_end"] += 1
    for w in outline["courses"][1]["units"][0]["weeks"]:
        w["n"] += 1
    with pytest.raises(ValidationError, match="expected"):
        validate_outline(outline)


def test_rejects_week_n_mismatch_inside_unit() -> None:
    outline = _valid_outline(4)
    outline["courses"][0]["units"][0]["weeks"][1]["n"] = 99
    with pytest.raises(ValidationError, match="expected global W"):
        validate_outline(outline)


def test_rejects_course_weeks_end_doesnt_match_units() -> None:
    outline = _valid_outline(4)
    outline["courses"][0]["weeks_end"] = 99
    with pytest.raises(ValidationError, match="weeks_end"):
        validate_outline(outline)


# ---------------------------------------------------------------------------
# Unit-level rejections
# ---------------------------------------------------------------------------


def test_rejects_unit_with_5_outcome_phrases() -> None:
    outline = _valid_outline(4)
    outline["courses"][0]["units"][0]["outcome_phrases"] = _verb_outcomes(5)
    with pytest.raises(ValidationError, match="6 outcome_phrases"):
        validate_outline(outline)


def test_rejects_unit_outcome_with_invalid_verb() -> None:
    outline = _valid_outline(4)
    outline["courses"][0]["units"][0]["outcome_phrases"][0] = "Understand thermodynamics"
    with pytest.raises(ValidationError, match="action verb"):
        validate_outline(outline)


def test_accepts_unit_outcome_with_comma_compound_verb() -> None:
    """``"Apply, compute, and compare..."`` passes — first verb gates."""
    outline = _valid_outline(4)
    outline["courses"][0]["units"][0]["outcome_phrases"][0] = (
        "Apply, compute, and compare two formulations of a system"
    )
    validate_outline(outline)


def test_rejects_unit_with_11_glossary_terms() -> None:
    outline = _valid_outline(4)
    outline["courses"][0]["units"][0]["glossary_terms"] = _terms("u1", 11)
    with pytest.raises(ValidationError, match="12 glossary_terms"):
        validate_outline(outline)


def test_rejects_duplicate_glossary_within_unit() -> None:
    outline = _valid_outline(4)
    outline["courses"][0]["units"][0]["glossary_terms"][0] = (
        outline["courses"][0]["units"][0]["glossary_terms"][1]
    )
    with pytest.raises(ValidationError, match="duplicate glossary_terms within unit"):
        validate_outline(outline)


def test_rejects_duplicate_unit_slugs_within_course() -> None:
    """A 2-unit course where both units share a slug."""
    outline = _valid_outline(4)
    # Add a second unit to course 1.
    outline["courses"][0]["units"].append(
        _standard_unit(slug=outline["courses"][0]["units"][0]["slug"],
                       n=2, weeks_start=4)
    )
    outline["courses"][0]["weeks_end"] += 3
    # Push the rest of the degree forward.
    for c in outline["courses"][1:]:
        c["weeks_start"] += 3
        c["weeks_end"] += 3
        for u in c["units"]:
            u["weeks_start"] += 3
            u["weeks_end"] += 3
            for w in u["weeks"]:
                w["n"] += 3
    with pytest.raises(ValidationError, match="duplicate unit slugs"):
        validate_outline(outline)


def test_rejects_capstone_inconsistency_unit_marked_but_course_not() -> None:
    outline = _valid_outline(4)
    outline["courses"][0]["units"][0]["is_capstone"] = True
    # Replace its weeks with capstone phases too so we hit the *course*
    # consistency check (otherwise the unit's own shape check fires first
    # because standard anchors are still present).
    outline["courses"][0]["units"][0].update(
        outcome_phrases=None, key_concepts=None, glossary_terms=None,
    )
    weeks_start = outline["courses"][0]["units"][0]["weeks_start"]
    outline["courses"][0]["units"][0]["weeks"] = [
        {"n": weeks_start, "slug": "p", "title": "P", "phase": "Proposal"},
        {"n": weeks_start + 1, "slug": "e", "title": "E", "phase": "Execution"},
        {"n": weeks_start + 2, "slug": "s", "title": "S", "phase": "Submission"},
    ]
    with pytest.raises(ValidationError, match="marked capstone"):
        validate_outline(outline)


# ---------------------------------------------------------------------------
# Week-level rejections
# ---------------------------------------------------------------------------


def test_rejects_standard_week_with_phase_set() -> None:
    """A standard (non-capstone) week must not set ``phase``."""
    outline = _valid_outline(4)
    outline["courses"][0]["units"][0]["weeks"][0]["phase"] = "Proposal"
    with pytest.raises(ValidationError, match="standard weeks must not set"):
        validate_outline(outline)


def test_rejects_capstone_week_missing_phase() -> None:
    outline = _valid_outline(4)
    # courses[-1] is the capstone course; unit[0] is the capstone unit.
    del outline["courses"][-1]["units"][0]["weeks"][0]["phase"]
    with pytest.raises(ValidationError, match="must set `phase`"):
        validate_outline(outline)


def test_rejects_capstone_week_with_invalid_phase_string() -> None:
    outline = _valid_outline(4)
    outline["courses"][-1]["units"][0]["weeks"][0]["phase"] = "Defense"
    with pytest.raises(ValidationError):
        validate_outline(outline)


def test_rejects_standard_week_with_only_2_outcome_phrases() -> None:
    outline = _valid_outline(4)
    outline["courses"][0]["units"][0]["weeks"][0]["outcome_phrases"] = _verb_outcomes(2)
    with pytest.raises(ValidationError, match="3 or 4 outcome_phrases"):
        validate_outline(outline)


def test_rejects_week_outcome_with_invalid_verb() -> None:
    outline = _valid_outline(4)
    outline["courses"][0]["units"][0]["weeks"][0]["outcome_phrases"][0] = (
        "Understand the lecture"
    )
    with pytest.raises(ValidationError, match="action verb"):
        validate_outline(outline)


def test_rejects_duplicate_week_slugs_within_unit() -> None:
    outline = _valid_outline(4)
    outline["courses"][0]["units"][0]["weeks"][1]["slug"] = (
        outline["courses"][0]["units"][0]["weeks"][0]["slug"]
    )
    with pytest.raises(ValidationError, match="duplicate week slugs"):
        validate_outline(outline)


def test_rejects_invalid_slug_format() -> None:
    """Slugs must match the regex (lowercase, underscore, starts with letter)."""
    outline = _valid_outline(4)
    outline["courses"][0]["units"][0]["slug"] = "Math-Foundations"
    with pytest.raises(ValidationError):
        validate_outline(outline)


# ---------------------------------------------------------------------------
# Course-level cross-unit rejections
# ---------------------------------------------------------------------------


def test_rejects_duplicate_glossary_terms_across_units_in_same_course() -> None:
    """Two units in the same course defining the same term → bulk-fill
    would silently contradict. Hard fail."""
    outline = _valid_outline(4)
    # Add a second unit to course 1 with overlapping glossary terms.
    second_unit = _standard_unit("second_unit", 2, 4)
    # Force a glossary collision.
    second_unit["glossary_terms"][0] = outline["courses"][0]["units"][0]["glossary_terms"][0]
    outline["courses"][0]["units"].append(second_unit)
    outline["courses"][0]["weeks_end"] += 3
    # Push the rest of the degree forward.
    for c in outline["courses"][1:]:
        c["weeks_start"] += 3
        c["weeks_end"] += 3
        for u in c["units"]:
            u["weeks_start"] += 3
            u["weeks_end"] += 3
            for w in u["weeks"]:
                w["n"] += 3
    with pytest.raises(ValidationError, match="duplicate glossary terms across units"):
        validate_outline(outline)


# ---------------------------------------------------------------------------
# Extra-field rejection (typo guard)
# ---------------------------------------------------------------------------


def test_rejects_unknown_field_at_degree_level() -> None:
    """`extra="forbid"` catches typos like `total_course` vs `total_courses`."""
    outline = _valid_outline(4)
    outline["degree"]["total_course"] = 5  # typo
    with pytest.raises(ValidationError):
        validate_outline(outline)
