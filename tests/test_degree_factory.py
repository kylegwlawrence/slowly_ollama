"""Tests for the Phase 24 degree factory (app/degree_factory.py).

Two layers:
  * Pure scaffolding — the deterministic tiling/reconcile/slug helpers, proven
    against the *real* schema validator (the keystone test).
  * Pipeline — the staged generators + build orchestrator with a MOCKED
    generate_json (no real Ollama), plus the SSE consumers.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from app import degree_factory as df
from app.degree_schema import SLUG_PATTERN, validate_outline


@pytest.fixture(autouse=True)
def _isolate_factory(monkeypatch) -> None:
    """Neutralize the rsync backup hook AND isolate the workspace so build /
    persistence tests never spawn real backups or write to the real
    ``agent_workspace`` (FILE_TOOL_ROOT defaults to ``./agent_workspace`` via
    ``.env``). Tests that need a workspace set FILE_TOOL_ROOT to a tmp_path
    themselves — that setenv wins over this delenv. Tests asserting the
    backup-failure path re-patch ``request_backup`` themselves."""
    monkeypatch.setattr(df.backup, "request_backup", lambda reason: None)
    monkeypatch.delenv("FILE_TOOL_ROOT", raising=False)


# ---------------------------------------------------------------------------
# Deterministic scaffolding
# ---------------------------------------------------------------------------


def test_tile_courses_dense_from_w1_and_last_capstone() -> None:
    courses = [{"week_count": 10}, {"week_count": 5}, {"week_count": 8}]
    df.tile_courses(courses)
    assert [(c["n"], c["weeks_start"], c["weeks_end"]) for c in courses] == [
        (1, 1, 10),
        (2, 11, 15),
        (3, 16, 23),
    ]
    assert [c["is_capstone"] for c in courses] == [False, False, True]


def test_tile_courses_clamps_nonpositive_week_count() -> None:
    courses = [{"week_count": 0}, {"week_count": -3}]
    df.tile_courses(courses)
    # each course gets at least one week, so the range never inverts
    for c in courses:
        assert c["weeks_end"] >= c["weeks_start"]


@pytest.mark.parametrize(
    "span,counts",
    [
        (12, [3, 3, 3]),   # exact
        (12, [10, 10, 10]),  # over -> trimmed
        (12, [1, 1, 1]),    # under -> padded
        (5, [1, 1, 1, 1]),  # span barely covers units
    ],
)
def test_reconcile_unit_week_counts_sums_to_span(span, counts) -> None:
    units = [{"week_count": c} for c in counts]
    df.reconcile_unit_week_counts(span, units)
    assert sum(u["week_count"] for u in units) == span
    assert all(u["week_count"] >= 1 for u in units)


def test_reconcile_raises_when_span_smaller_than_unit_count() -> None:
    units = [{"week_count": 1} for _ in range(5)]
    with pytest.raises(df.DegreeFactoryError):
        df.reconcile_unit_week_counts(3, units)


def test_tile_units_tiles_the_course_span() -> None:
    units = [{"week_count": 2}, {"week_count": 3}]
    df.tile_units(11, units)  # course starts at global week 11
    assert [(u["n"], u["weeks_start"], u["weeks_end"]) for u in units] == [
        (1, 11, 12),
        (2, 13, 15),
    ]


def test_assign_week_numbers_is_global_dense() -> None:
    weeks = [{}, {}, {}]
    df.assign_week_numbers(7, weeks)
    assert [w["n"] for w in weeks] == [7, 8, 9]


@pytest.mark.parametrize("span", [1, 2, 3, 4, 5, 8, 12])
def test_phase_sequence_covers_span(span) -> None:
    phases = df._phase_sequence(span)
    assert len(phases) == span
    assert set(phases) <= {"Proposal", "Execution", "Submission"}
    if span >= 3:  # all three phases present once the capstone is long enough
        assert set(phases) == {"Proposal", "Execution", "Submission"}


def test_fit_week_objects_truncates_extras() -> None:
    unit = {"weeks_start": 1, "weeks_end": 2, "glossary_terms": [f"g{i}" for i in range(12)]}
    weeks = [{"slug": f"w{i}"} for i in range(5)]
    fitted = df._fit_week_objects(unit, weeks)
    assert len(fitted) == 2


def test_fit_week_objects_pads_with_valid_synth_weeks() -> None:
    unit = {
        "weeks_start": 1, "weeks_end": 3, "title": "U",
        "glossary_terms": [f"g{i}" for i in range(12)],
    }
    fitted = df._fit_week_objects(unit, [{"slug": "w0", "title": "W0",
                                          "outcome_phrases": ["Apply a", "Analyze b", "Explain c"],
                                          "key_term_names": [f"k{i}" for i in range(6)]}])
    assert len(fitted) == 3
    # the two synthesized weeks are schema-shaped standard weeks
    assert df.content_errors_weeks(fitted) == []


# ---------------------------------------------------------------------------
# Slug normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["Vector Calculus", "  data--science  ", "Quantum/Mechanics!", "3D printing", ""],
)
def test_slugify_always_matches_schema_pattern(raw) -> None:
    slug = df._slugify(raw, fallback="item")
    assert re.match(SLUG_PATTERN, slug), f"{slug!r} from {raw!r}"


def test_assign_unique_slugs_dedupes_collisions() -> None:
    items = [{"title": "Calculus"}, {"title": "Calculus"}, {"title": "Calculus"}]
    df._assign_unique_slugs(items, fallback="unit")
    slugs = [it["slug"] for it in items]
    assert len(set(slugs)) == 3
    assert all(re.match(SLUG_PATTERN, s) for s in slugs)


# ---------------------------------------------------------------------------
# KEYSTONE: a fully scaffolded outline passes the real schema validator
# ---------------------------------------------------------------------------


def _content_unit(course_idx: int, unit_idx: int, week_count: int) -> dict:
    """A schema-valid standard unit with course-unique glossary terms."""
    tag = f"c{course_idx}u{unit_idx}"
    return {
        "slug": f"{tag}",
        "title": f"Unit {unit_idx}",
        "focus": "unit focus",
        "week_count": week_count,
        "outcome_phrases": [f"Apply concept {i}" for i in range(6)],
        "key_concepts": [f"{tag}_kc{i}" for i in range(8)],
        "glossary_terms": [f"{tag}_gl{i}" for i in range(12)],
    }


def _scaffold_outline(course_week_counts, units_per_course) -> dict:
    """Mirror the orchestrator's deterministic steps with stub content.

    Builds courses -> units -> weeks using only the factory's tiling helpers,
    so a passing validate_outline proves the structure is correct.
    """
    courses = [
        {"slug": f"course{i}", "title": f"Course {i}", "focus": "focus",
         "tier": "intro", "key_capability": "cap", "week_count": wc}
        for i, wc in enumerate(course_week_counts, start=1)
    ]
    df.tile_courses(courses)
    out_courses = []
    for ci, course in enumerate(courses):
        span = course["weeks_end"] - course["weeks_start"] + 1
        if course["is_capstone"]:
            phases = df._phase_sequence(span)
            weeks = [{"title": f"week {i}", "phase": phases[i], "slug": phases[i].lower()}
                     for i in range(span)]
            df._assign_unique_slugs(weeks, fallback="week")
            df.assign_week_numbers(course["weeks_start"], weeks)
            unit = {
                "n": 1, "slug": "capstone", "title": "Capstone",
                "weeks_start": course["weeks_start"], "weeks_end": course["weeks_end"],
                "is_capstone": True,
                "weeks": [df._pick(w, df._WEEK_FIELDS) for w in weeks],
            }
            course["units"] = [unit]
        else:
            n_units = units_per_course[ci]
            units = [_content_unit(ci, j, 1) for j in range(n_units)]
            df._assign_unique_slugs(units, fallback="unit")
            df.reconcile_unit_week_counts(span, units)
            df.tile_units(course["weeks_start"], units)
            for u in units:
                target = u["weeks_end"] - u["weeks_start"] + 1
                weeks = [{
                    "slug": f"{u['slug']}w{i}", "title": f"Week {i}", "focus": "f",
                    "outcome_phrases": ["Apply a", "Analyze b", "Explain c"],
                    "key_term_names": [f"{u['slug']}kt{k}" for k in range(6)],
                } for i in range(target)]
                df._assign_unique_slugs(weeks, fallback="week")
                df.assign_week_numbers(u["weeks_start"], weeks)
                u["weeks"] = [df._pick(w, df._WEEK_FIELDS) for w in weeks]
            course["units"] = [df._pick(u, df._UNIT_FIELDS) for u in units]
        out_courses.append(df._pick(course, df._COURSE_FIELDS))

    return {
        "version": 1,
        "degree": {
            "slug": "degree", "title": "Degree", "tier_reached": "advanced",
            "prerequisites": "none", "themes": [f"theme {i}" for i in range(4)],
            "program_outcome_phrases": [f"Derive result {i}" for i in range(6)],
            "total_courses": len(out_courses),
        },
        "courses": out_courses,
    }


@pytest.mark.parametrize(
    "course_week_counts,units_per_course",
    [
        ([10, 12, 8, 6], [3, 4, 2, None]),          # 4 courses
        ([8, 10, 12, 9, 6], [2, 3, 4, 3, None]),    # 5 courses
        ([8, 8, 10, 12, 9, 6], [2, 2, 3, 4, 3, None]),  # 6 courses
        ([20, 5, 7, 4], [7, 1, 2, None]),           # uneven unit counts
    ],
)
def test_scaffolded_outline_passes_validate_dense_week_numbering(
    course_week_counts, units_per_course
) -> None:
    outline = _scaffold_outline(course_week_counts, units_per_course)
    # Must not raise — proves server-owned structure satisfies every validator.
    validate_outline(outline)
    # And it survives a JSON round-trip (what gets written to disk).
    validate_outline(json.loads(json.dumps(outline)))


# ---------------------------------------------------------------------------
# Content gates
# ---------------------------------------------------------------------------


def test_content_errors_stage_a_flags_bad_counts_and_verbs() -> None:
    errs = df.content_errors_stage_a(
        {"themes": ["a", "b"], "program_outcome_phrases": ["Understand x"] * 6,
         "courses": [{}]},
        course_count=4,
    )
    joined = " ".join(errs)
    assert "4 themes" in joined
    assert "action verb" in joined
    assert "4 courses" in joined


def test_content_errors_units_flags_glossary_collision_across_units() -> None:
    unit_a = _content_unit(0, 0, 1)
    unit_b = _content_unit(0, 1, 1)
    unit_b["glossary_terms"][0] = unit_a["glossary_terms"][0]  # shared term
    errs = df.content_errors_units([unit_a, unit_b])
    assert any("unique across the whole course" in e for e in errs)


def test_content_errors_weeks_flags_non_verb_and_bad_counts() -> None:
    errs = df.content_errors_weeks([
        {"title": "W", "outcome_phrases": ["Understand things", "Apply x"],
         "key_term_names": ["a", "b"]},
    ])
    joined = " ".join(errs)
    assert "action verb" in joined
    assert "3 or 4 outcome_phrases" in joined
    assert "6 to 8 key_term_names" in joined


def test_content_errors_clean_input_returns_empty() -> None:
    assert df.content_errors_units([_content_unit(0, 0, 1)]) == []


# ---------------------------------------------------------------------------
# Pipeline — staged generation + build orchestrator, with MOCKED generate_json
# ---------------------------------------------------------------------------


FORM = df.DegreeForm(
    subject="Classical mechanics",
    learner="hobbyist with calculus",
    tier_benchmark="Goldstein, Classical Mechanics",
    capstone="a written thesis",
    course_count=4,
)


def _ok_stage_a(course_count: int) -> dict:
    return {
        "slug": "Classical Mechanics",  # exercises slugify
        "title": "Classical Mechanics",
        "tier_reached": "advanced",
        "prerequisites": "single-variable calculus",
        "themes": [f"tension {i}" for i in range(4)],
        "program_outcome_phrases": [f"Derive result {i}" for i in range(6)],
        "courses": [
            {"slug": f"course {i}", "title": f"Course {i}", "focus": "f",
             "tier": "intro", "key_capability": "k", "week_count": 6}
            for i in range(course_count)
        ],
    }


def _ok_units(n: int) -> list[dict]:
    return [{
        "slug": f"u{j}", "title": f"Unit {j}", "focus": "f", "week_count": 3,
        "outcome_phrases": [f"Apply idea {i}" for i in range(6)],
        "key_concepts": [f"u{j}kc{i}" for i in range(8)],
        "glossary_terms": [f"u{j}gl{i}" for i in range(12)],
    } for j in range(n)]


def _ok_weeks(n: int) -> list[dict]:
    return [{
        "slug": f"w{i}", "title": f"Week {i}", "focus": "f",
        "outcome_phrases": ["Apply a", "Analyze b", "Explain c"],
        "key_term_names": [f"kt{k}" for k in range(6)],
    } for i in range(n)]


def _make_fake(*, raise_on_units=False, bad_weeks_first=False, raise_on_capstone=False):
    """Build a fake `generate_json` that returns valid per-stage content.

    Stage is detected from the format schema's top-level property keys.
    """
    state = {"weeks_calls": 0, "unit_calls": 0}

    async def fake(client, model, *, system, user, format_schema=None,
                   num_ctx=None, **kwargs):
        props = (format_schema or {}).get("properties", {})
        if "courses" in props:
            return _ok_stage_a(props["courses"]["minItems"])
        if "units" in props:
            state["unit_calls"] += 1
            if raise_on_units:
                raise df.OllamaUnavailable("ollama down")
            return {"units": _ok_units(2)}
        if "weeks" in props:
            state["weeks_calls"] += 1
            target = props["weeks"]["minItems"]
            weeks = _ok_weeks(target)
            if bad_weeks_first and state["weeks_calls"] == 1:
                weeks[0]["outcome_phrases"] = ["Understand things", "Apply x", "Explain y"]
            return {"weeks": weeks}
        if "week_titles" in props:
            if raise_on_capstone:
                raise df.OllamaProtocolError("garbage capstone")
            span = props["week_titles"]["minItems"]
            return {"title": "Capstone", "focus": "thesis",
                    "unit_title": "Capstone", "week_titles": [f"phase week {i}" for i in range(span)]}
        return {}

    fake.state = state
    return fake


@pytest.mark.asyncio
async def test_create_draft_runs_stage_a_and_tiles(monkeypatch) -> None:
    monkeypatch.setattr(df, "generate_json", _make_fake())
    draft = await df.create_draft(None, FORM, model="m")
    assert len(draft.courses) == 4
    assert draft.courses[0]["weeks_start"] == 1
    assert draft.courses[-1]["is_capstone"] is True
    assert re.match(SLUG_PATTERN, draft.degree_meta["slug"])
    assert df.degree_drafts[draft.draft_id] is draft


@pytest.mark.asyncio
async def test_regenerate_draft_updates_in_place(monkeypatch) -> None:
    monkeypatch.setattr(df, "generate_json", _make_fake())
    draft = await df.create_draft(None, FORM, model="m")
    draft_id = draft.draft_id
    out = await df.regenerate_draft(None, draft, note="add more depth", model="m")
    assert out.draft_id == draft_id
    assert df.degree_drafts[draft_id] is out
    assert len(out.courses) == 4


@pytest.mark.asyncio
async def test_run_build_writes_valid_outline(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    monkeypatch.setattr(df, "generate_json", _make_fake())
    draft = await df.create_draft(None, FORM, model="m")
    job = df.DegreeJob(job_id="j1", degree_slug=draft.degree_meta["slug"])
    await df.run_build(job, client=None, draft=draft, model="m")
    assert job.done is True
    assert job.error is None
    assert job.result_path is not None
    validate_outline(Path(job.result_path))  # written file is schema-valid
    events = [e for e, _ in job.events]
    assert "done" in events and "error" not in events


@pytest.mark.asyncio
async def test_run_build_retries_then_succeeds(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    fake = _make_fake(bad_weeks_first=True)
    monkeypatch.setattr(df, "generate_json", fake)
    draft = await df.create_draft(None, FORM, model="m")
    job = df.DegreeJob(job_id="j2", degree_slug=draft.degree_meta["slug"])
    await df.run_build(job, client=None, draft=draft, model="m")
    assert job.error is None
    assert job.result_path is not None
    assert fake.state["weeks_calls"] >= 2  # first weeks call failed -> retried


@pytest.mark.asyncio
async def test_run_build_retries_then_succeeds_on_malformed_json(
    monkeypatch, tmp_path
) -> None:
    """First weeks call raises a `generate_json:` protocol error (truncated
    string); the retry loop should feed it back and the second call succeeds."""
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    calls = {"weeks": 0}

    async def fake(client, model, *, system, user, format_schema=None,
                   num_ctx=None, **kwargs):
        props = (format_schema or {}).get("properties", {})
        if "courses" in props:
            return _ok_stage_a(props["courses"]["minItems"])
        if "units" in props:
            return {"units": _ok_units(2)}
        if "weeks" in props:
            calls["weeks"] += 1
            if calls["weeks"] == 1:
                raise df.OllamaProtocolError(
                    "generate_json: model content was not valid JSON: "
                    "Unterminated string starting at: line 38 column 9 (char 1378)"
                )
            return {"weeks": _ok_weeks(props["weeks"]["minItems"])}
        if "week_titles" in props:
            span = props["week_titles"]["minItems"]
            return {"title": "C", "focus": "f", "unit_title": "U",
                    "week_titles": [f"t{i}" for i in range(span)]}
        return {}

    monkeypatch.setattr(df, "generate_json", fake)
    draft = await df.create_draft(None, FORM, model="m")
    job = df.DegreeJob(job_id="jjson1", degree_slug=draft.degree_meta["slug"])
    await df.run_build(job, client=None, draft=draft, model="m")
    assert job.error is None
    assert calls["weeks"] >= 2  # retried after malformed-JSON failure


@pytest.mark.asyncio
async def test_generate_checked_does_not_retry_envelope_errors(
    monkeypatch, tmp_path
) -> None:
    """Envelope-shape errors (Ollama itself returned a malformed /api/chat
    response) must NOT be retried — only the model-side `generate_json:`
    prefix opts in. The build surfaces the error immediately."""
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    calls = {"units": 0}

    async def fake(client, model, *, system, user, format_schema=None,
                   num_ctx=None, **kwargs):
        props = (format_schema or {}).get("properties", {})
        if "courses" in props:
            return _ok_stage_a(props["courses"]["minItems"])
        if "units" in props:
            calls["units"] += 1
            raise df.OllamaProtocolError(
                "Ollama returned an unexpected /api/chat shape: KeyError('message')"
            )
        return {}

    monkeypatch.setattr(df, "generate_json", fake)
    draft = await df.create_draft(None, FORM, model="m")
    job = df.DegreeJob(job_id="jenv1", degree_slug=draft.degree_meta["slug"])
    await df.run_build(job, client=None, draft=draft, model="m")
    assert job.error is not None
    assert calls["units"] == 1  # one call, no retry


@pytest.mark.asyncio
async def test_run_build_reconciles_absurd_unit_week_counts(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))

    async def fake(client, model, *, system, user, format_schema=None, num_ctx=None, **kwargs):
        props = (format_schema or {}).get("properties", {})
        if "courses" in props:
            return _ok_stage_a(props["courses"]["minItems"])
        if "units" in props:
            units = _ok_units(2)
            for u in units:
                u["week_count"] = 100  # nowhere near the course span
            return {"units": units}
        if "weeks" in props:
            return {"weeks": _ok_weeks(props["weeks"]["minItems"])}
        if "week_titles" in props:
            span = props["week_titles"]["minItems"]
            return {"title": "C", "focus": "f", "unit_title": "U",
                    "week_titles": [f"t{i}" for i in range(span)]}
        return {}

    monkeypatch.setattr(df, "generate_json", fake)
    draft = await df.create_draft(None, FORM, model="m")
    job = df.DegreeJob(job_id="j3", degree_slug=draft.degree_meta["slug"])
    await df.run_build(job, client=None, draft=draft, model="m")
    assert job.error is None
    validate_outline(Path(job.result_path))  # reconcile saved it


@pytest.mark.asyncio
async def test_run_build_surfaces_error_and_writes_no_file(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))

    async def fake(client, model, *, system, user, format_schema=None, num_ctx=None, **kwargs):
        props = (format_schema or {}).get("properties", {})
        if "courses" in props:
            return _ok_stage_a(props["courses"]["minItems"])
        if "units" in props:
            return {"units": _ok_units(2)}
        if "weeks" in props:  # always non-verb -> retries exhausted
            weeks = _ok_weeks(props["weeks"]["minItems"])
            weeks[0]["outcome_phrases"] = ["Understand a", "Know b", "Learn c"]
            return {"weeks": weeks}
        if "week_titles" in props:
            span = props["week_titles"]["minItems"]
            return {"title": "C", "focus": "f", "unit_title": "U",
                    "week_titles": [f"t{i}" for i in range(span)]}
        return {}

    monkeypatch.setattr(df, "generate_json", fake)
    draft = await df.create_draft(None, FORM, model="m")
    job = df.DegreeJob(job_id="j4", degree_slug=draft.degree_meta["slug"])
    await df.run_build(job, client=None, draft=draft, model="m")
    assert job.error is not None
    assert "error" in [e for e, _ in job.events]
    assert job.result_path is None
    assert not (tmp_path / draft.degree_meta["slug"] / "degree_outline.json").exists()


@pytest.mark.asyncio
async def test_run_build_ollama_unavailable_sets_error(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    monkeypatch.setattr(df, "generate_json", _make_fake(raise_on_units=True))
    draft = await df.create_draft(None, FORM, model="m")  # stage A still succeeds
    job = df.DegreeJob(job_id="j5", degree_slug=draft.degree_meta["slug"])
    await df.run_build(job, client=None, draft=draft, model="m")
    assert job.error is not None
    assert "error" in [e for e, _ in job.events]
    assert job.result_path is None


@pytest.mark.asyncio
async def test_run_build_validation_failure_path(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    monkeypatch.setattr(df, "generate_json", _make_fake())

    def boom(_outline):
        raise ValueError("forced validation failure")

    monkeypatch.setattr(df, "validate_outline", boom)
    draft = await df.create_draft(None, FORM, model="m")
    job = df.DegreeJob(job_id="j6", degree_slug=draft.degree_meta["slug"])
    await df.run_build(job, client=None, draft=draft, model="m")
    assert job.error is not None
    assert "error" in [e for e, _ in job.events]
    assert job.result_path is None
    assert not (tmp_path / draft.degree_meta["slug"] / "degree_outline.json").exists()


@pytest.mark.asyncio
async def test_capstone_falls_back_when_titles_fail(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    monkeypatch.setattr(df, "generate_json", _make_fake(raise_on_capstone=True))
    draft = await df.create_draft(None, FORM, model="m")
    job = df.DegreeJob(job_id="j7", degree_slug=draft.degree_meta["slug"])
    await df.run_build(job, client=None, draft=draft, model="m")
    assert job.error is None  # capstone title failure is non-fatal
    validate_outline(Path(job.result_path))


@pytest.mark.asyncio
async def test_run_build_unconfigured_workspace_errors(monkeypatch) -> None:
    monkeypatch.delenv("FILE_TOOL_ROOT", raising=False)
    monkeypatch.setattr(df, "generate_json", _make_fake())
    draft = await df.create_draft(None, FORM, model="m")
    job = df.DegreeJob(job_id="j8", degree_slug=draft.degree_meta["slug"])
    await df.run_build(job, client=None, draft=draft, model="m")
    assert job.error is not None
    assert job.result_path is None


@pytest.mark.asyncio
async def test_start_build_registers_and_completes(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    monkeypatch.setattr(df, "generate_json", _make_fake())
    draft = await df.create_draft(None, FORM, model="m")
    job = await df.start_build(client=None, draft=draft, model="m")
    assert df.degree_jobs[job.job_id] is job
    await job.task  # the registry owns the task; wait for it
    assert job.done is True
    assert job.result_path is not None


# ---------------------------------------------------------------------------
# Per-course build loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_course_build_builds_one_and_emits_review(monkeypatch) -> None:
    monkeypatch.setattr(df, "generate_json", _make_fake())
    draft = await df.create_draft(None, FORM, model="m")  # 4 courses
    job = df.DegreeJob(job_id="jc1", degree_slug=draft.degree_meta["slug"])
    await df.run_course_build(job, client=None, draft=draft, model="m")
    assert job.error is None
    assert job.built_course is not None
    assert job.course_index == 0
    # the build does NOT approve/append — that's the route's job on user action
    assert draft.built_courses == []
    # the terminal 'done' event carries the review fragment + approve control
    done = [p for e, p in job.events if e == "done"]
    assert done and "Approve" in done[0]
    assert f"/degrees/draft/{draft.draft_id}/approve/jc1" in done[0]


@pytest.mark.asyncio
async def test_run_course_build_ollama_error_sets_error(monkeypatch) -> None:
    monkeypatch.setattr(df, "generate_json", _make_fake(raise_on_units=True))
    draft = await df.create_draft(None, FORM, model="m")  # stage A still ok
    job = df.DegreeJob(job_id="jce", degree_slug=draft.degree_meta["slug"])
    await df.run_course_build(job, client=None, draft=draft, model="m")
    assert job.error is not None
    assert job.built_course is None
    assert "error" in [e for e, _ in job.events]


@pytest.mark.asyncio
async def test_run_course_build_factory_error_sets_error(monkeypatch) -> None:
    async def fake(client, model, *, system, user, format_schema=None, num_ctx=None, **kw):
        props = (format_schema or {}).get("properties", {})
        if "courses" in props:
            return _ok_stage_a(props["courses"]["minItems"])
        if "units" in props:
            return {"units": _ok_units(2)}
        if "weeks" in props:  # always non-verb -> retries exhaust -> DegreeFactoryError
            weeks = _ok_weeks(props["weeks"]["minItems"])
            weeks[0]["outcome_phrases"] = ["Understand a", "Know b", "Learn c"]
            return {"weeks": weeks}
        return {}

    monkeypatch.setattr(df, "generate_json", fake)
    draft = await df.create_draft(None, FORM, model="m")
    job = df.DegreeJob(job_id="jcf", degree_slug=draft.degree_meta["slug"])
    await df.run_course_build(job, client=None, draft=draft, model="m")
    assert job.error is not None
    assert job.built_course is None
    assert "error" in [e for e, _ in job.events]


@pytest.mark.asyncio
async def test_start_course_build_registers_and_completes(monkeypatch) -> None:
    monkeypatch.setattr(df, "generate_json", _make_fake())
    draft = await df.create_draft(None, FORM, model="m")
    job = await df.start_course_build(client=None, draft=draft, model="m")
    assert df.degree_jobs[job.job_id] is job
    await job.task  # the registry owns the task; wait for it
    assert job.built_course is not None
    assert job.course_index == 0


@pytest.mark.asyncio
async def test_per_course_loop_to_completion_validates(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    monkeypatch.setattr(df, "generate_json", _make_fake())
    draft = await df.create_draft(None, FORM, model="m")
    total = len(draft.courses)
    # Drive the loop: build each course, then simulate the user's approval.
    for i in range(total):
        job = df.DegreeJob(job_id=f"jc{i}", degree_slug=draft.degree_meta["slug"])
        await df.run_course_build(job, client=None, draft=draft, model="m")
        assert job.built_course is not None and job.course_index == i
        draft.built_courses.append(job.built_course)  # = approve
    path = df.assemble_and_write(draft)
    validate_outline(Path(path))  # the loop's output is a valid outline


@pytest.mark.asyncio
async def test_run_build_resumes_from_partial_built_courses(monkeypatch, tmp_path) -> None:
    """run_build (the 'build all remaining' path) only builds courses past
    those already approved."""
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    monkeypatch.setattr(df, "generate_json", _make_fake())
    draft = await df.create_draft(None, FORM, model="m")
    # Approve the first course up front, then let run_build finish the rest.
    first = df.DegreeJob(job_id="jf", degree_slug=draft.degree_meta["slug"])
    await df.run_course_build(first, client=None, draft=draft, model="m")
    draft.built_courses.append(first.built_course)
    job = df.DegreeJob(job_id="jrest", degree_slug=draft.degree_meta["slug"])
    await df.run_build(job, client=None, draft=draft, model="m")
    assert job.error is None
    assert len(draft.built_courses) == len(draft.courses)
    validate_outline(Path(job.result_path))


def test_assemble_and_write_rejects_incomplete_outline(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    # built_courses holds garbage -> validate_outline raises (not DegreeFactoryError)
    draft = df.DegreeDraft(
        draft_id="x", form=FORM,
        degree_meta={"slug": "d", "title": "D", "tier_reached": "intro",
                     "prerequisites": "none", "themes": ["a", "b", "c", "d"],
                     "program_outcome_phrases": ["Derive x"] * 6},
        courses=[],
    )
    draft.built_courses = [{"bogus": True}]
    with pytest.raises(Exception):  # pydantic.ValidationError
        df.assemble_and_write(draft)


# ---------------------------------------------------------------------------
# Incremental persistence + resume
# ---------------------------------------------------------------------------


def _draft_with(slug="physics", built=0, total=4):
    return df.DegreeDraft(
        draft_id="d", form=FORM,
        degree_meta={"slug": slug, "title": "Physics", "tier_reached": "advanced",
                     "prerequisites": "calc", "themes": ["a", "b", "c", "d"],
                     "program_outcome_phrases": ["Derive x"] * 6},
        courses=[{"slug": f"c{i}", "title": f"C{i}", "week_count": 6} for i in range(total)],
        built_courses=[{"slug": f"b{i}", "title": f"Built {i}", "units": []}
                       for i in range(built)],
    )


def test_persist_and_load_partial_round_trip(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    draft = _draft_with(built=1, total=4)
    df.persist_draft(draft)
    assert (tmp_path / "physics" / df.PARTIAL_NAME).is_file()

    loaded = df.load_partial("physics")
    assert loaded is not None
    assert loaded.draft_id != "d"  # fresh in-memory id
    assert loaded.degree_meta["title"] == "Physics"
    assert len(loaded.built_courses) == 1
    assert len(loaded.courses) == 4
    assert loaded.form.subject == FORM.subject  # form round-trips
    assert df.degree_drafts[loaded.draft_id] is loaded  # registered


def test_load_partial_missing_returns_none(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    assert df.load_partial("ghost") is None


def test_persist_and_load_noop_when_unconfigured(monkeypatch) -> None:
    monkeypatch.delenv("FILE_TOOL_ROOT", raising=False)
    df.persist_draft(_draft_with())  # must not raise
    assert df.load_partial("physics") is None


def test_load_partial_corrupt_returns_none(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    (tmp_path / "physics").mkdir()
    (tmp_path / "physics" / df.PARTIAL_NAME).write_text("{ not json")
    assert df.load_partial("physics") is None


@pytest.mark.asyncio
async def test_create_draft_persists_checkpoint(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    monkeypatch.setattr(df, "generate_json", _make_fake())
    draft = await df.create_draft(None, FORM, model="m")
    assert (tmp_path / draft.degree_meta["slug"] / df.PARTIAL_NAME).is_file()


@pytest.mark.asyncio
async def test_run_build_deletes_partial_on_finalize(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    monkeypatch.setattr(df, "generate_json", _make_fake())
    draft = await df.create_draft(None, FORM, model="m")
    slug = draft.degree_meta["slug"]
    assert (tmp_path / slug / df.PARTIAL_NAME).is_file()  # checkpoint saved
    job = df.DegreeJob(job_id="jp", degree_slug=slug)
    await df.run_build(job, client=None, draft=draft, model="m")
    assert job.error is None
    assert (tmp_path / slug / df.OUTLINE_NAME).is_file()         # final written
    assert not (tmp_path / slug / df.PARTIAL_NAME).is_file()      # partial removed


@pytest.mark.asyncio
async def test_run_course_build_guard_when_all_built(monkeypatch) -> None:
    monkeypatch.setattr(df, "generate_json", _make_fake())
    draft = await df.create_draft(None, FORM, model="m")
    draft.built_courses = list(draft.courses)  # pretend everything is built
    job = df.DegreeJob(job_id="jg", degree_slug=draft.degree_meta["slug"])
    await df.run_course_build(job, client=None, draft=draft, model="m")
    assert job.error is not None
    assert job.built_course is None


# ---------------------------------------------------------------------------
# Slug uniqueness (versioning)
# ---------------------------------------------------------------------------


def test_unique_degree_slug_free_returns_base(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    assert df._unique_degree_slug("brand_new") == "brand_new"


def test_unique_degree_slug_appends_version(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    (tmp_path / "classical_mechanics").mkdir()
    assert df._unique_degree_slug("classical_mechanics") == "classical_mechanics_v2"
    (tmp_path / "classical_mechanics_v2").mkdir()
    assert df._unique_degree_slug("classical_mechanics") == "classical_mechanics_v3"


def test_unique_degree_slug_unconfigured_returns_base(monkeypatch) -> None:
    monkeypatch.delenv("FILE_TOOL_ROOT", raising=False)
    assert df._unique_degree_slug("anything") == "anything"


def test_unique_degree_slug_respects_length_cap(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    base = "a" * 41  # the maximum-length slug
    (tmp_path / base).mkdir()
    out = df._unique_degree_slug(base)
    assert out.endswith("_v2") and len(out) <= 41
    assert re.match(SLUG_PATTERN, out)


@pytest.mark.asyncio
async def test_create_draft_versions_slug_when_taken(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    monkeypatch.setattr(df, "generate_json", _make_fake())
    (tmp_path / "classical_mechanics").mkdir()  # an earlier degree owns the slug
    draft = await df.create_draft(None, FORM, model="m")
    assert draft.degree_meta["slug"] == "classical_mechanics_v2"


@pytest.mark.asyncio
async def test_regenerate_keeps_existing_slug(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    monkeypatch.setattr(df, "generate_json", _make_fake())
    draft = await df.create_draft(None, FORM, model="m")
    original = draft.degree_meta["slug"]  # create_draft persisted -> dir now exists
    out = await df.regenerate_draft(None, draft, note="more depth", model="m")
    # must NOT version to _v2 just because its own directory exists
    assert out.degree_meta["slug"] == original


# ---------------------------------------------------------------------------
# SSE consumers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consume_degree_job_replays_then_returns() -> None:
    job = df.DegreeJob(job_id="jx", degree_slug="s")
    job.events.append(("progress", "<div>working</div>"))
    job.events.append(("done", "<div>finished</div>"))
    job.done = True
    blob = "".join([c async for c in df.consume_degree_job(job)])
    assert "event: progress" in blob
    assert "event: done" in blob
    assert blob.index("event: progress") < blob.index("event: done")


@pytest.mark.asyncio
async def test_consume_degree_job_tails_live_events() -> None:
    job = df.DegreeJob(job_id="jt", degree_slug="s")
    collected: list[str] = []

    async def consume() -> None:
        async for c in df.consume_degree_job(job):
            collected.append(c)

    async def produce() -> None:
        await asyncio.sleep(0)  # let the consumer attach + wait on cond
        await df._emit(job, "progress", "<p>step</p>")
        await df._signal_done(job)

    await asyncio.gather(consume(), produce())
    assert any("event: progress" in c for c in collected)


@pytest.mark.asyncio
async def test_consume_degree_job_finished_emits_done() -> None:
    blob = "".join([c async for c in df.consume_degree_job_finished("zzz")])
    assert "event: done" in blob
    assert "degree-build-zzz" in blob


# ---------------------------------------------------------------------------
# Defensive branches (cheap pure-function coverage)
# ---------------------------------------------------------------------------


def test_starts_with_verb_rejects_blank() -> None:
    assert df._starts_with_verb("") is False
    assert df._starts_with_verb("   ") is False


def test_reconcile_no_units_is_noop() -> None:
    units: list[dict] = []
    df.reconcile_unit_week_counts(5, units)
    assert units == []


def test_normalize_course_week_counts_handles_non_int() -> None:
    courses = [{"week_count": "abc"}, {"week_count": None}, {"week_count": 999}]
    df._normalize_course_week_counts(courses)
    assert all(
        df._MIN_COURSE_WEEKS <= c["week_count"] <= df._MAX_COURSE_WEEKS
        for c in courses
    )


def test_content_errors_stage_a_counts_program_outcomes() -> None:
    errs = df.content_errors_stage_a(
        {"themes": ["a", "b", "c", "d"],
         "program_outcome_phrases": ["Derive x"] * 5,  # 5, not 6
         "courses": [{}] * 4},
        course_count=4,
    )
    assert any("6 program_outcome_phrases" in e for e in errs)


def test_content_errors_units_empty_list() -> None:
    assert any("at least one unit" in e for e in df.content_errors_units([]))


def test_content_errors_units_flags_non_verb_outcome() -> None:
    unit = _content_unit(0, 0, 1)
    unit["outcome_phrases"][0] = "Understand the basics"  # not an action verb
    errs = df.content_errors_units([unit])
    assert any("action verb" in e for e in errs)
    # The retry note must re-state the allowed verbs so the model can fix it.
    assert any("Apply" in e and "Derive" in e for e in errs)


def test_content_errors_units_flags_each_count() -> None:
    bad = {
        "title": "U",
        "outcome_phrases": ["Apply a"] * 5,   # 5, not 6 (still verb-gated)
        "key_concepts": ["c"] * 7,            # 7, not 8
        "glossary_terms": ["g"] * 11,         # 11, not 12 — and all identical
    }
    joined = " ".join(df.content_errors_units([bad]))
    assert "6 outcome_phrases" in joined
    assert "8 key_concepts" in joined
    assert "12 glossary_terms" in joined
    assert "unique within the unit" in joined


def test_write_outline_unconfigured_raises(monkeypatch) -> None:
    monkeypatch.delenv("FILE_TOOL_ROOT", raising=False)
    with pytest.raises(df.DegreeFactoryError):
        df._write_outline("x", {"version": 1})


def test_write_outline_rejects_escaping_slug(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    with pytest.raises(df.DegreeFactoryError):
        df._write_outline("../escape", {"version": 1})


@pytest.mark.asyncio
async def test_run_build_survives_backup_failure(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    monkeypatch.setattr(df, "generate_json", _make_fake())

    def _boom(reason):
        raise RuntimeError("rsync unreachable")

    monkeypatch.setattr(df.backup, "request_backup", _boom)
    draft = await df.create_draft(None, FORM, model="m")
    job = df.DegreeJob(job_id="jbk", degree_slug=draft.degree_meta["slug"])
    await df.run_build(job, client=None, draft=draft, model="m")
    assert job.error is None  # backup failure is non-fatal
    assert job.result_path is not None
