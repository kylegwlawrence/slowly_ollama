"""Phase 24: form-driven, low-context degree-outline factory.

Replaces the chat-based Degree Architect (Phase 23 Phase A) — which re-sent the
whole growing conversation every turn and overloaded the machine — with a
**staged pipeline of small, independent generations**. The user submits a tiny
form; Stage A proposes a degree skeleton + course list (the checkpoint); on
approval the build fills units (one call per course) and weeks (one call per
unit), assembles the outline, validates it against
:mod:`app.degree_schema`, and writes ``<slug>/degree_outline.json``.

Design principle — **the server owns structure, the model owns content**. Every
fragile structural rule the schema enforces (dense global week numbering, course
/unit/week tiling, capstone placement, exact array counts, slug shape and
uniqueness) is computed here in Python; the model only ever produces creative
content for one node at a time. This keeps each Ollama call's prompt small (one
course or one unit, never the whole degree) and removes essentially the entire
class of schema-validation failures.

State is in-memory (single-process app, mirroring
:data:`app.generation.live_generations`): :data:`degree_drafts` holds the
Stage-A result between the checkpoint and the build; :data:`degree_jobs` holds
the running build, owned by an :class:`asyncio.Task` (not the HTTP request), so a
page reload never cancels it. The durable artifact is the JSON on disk — if the
server restarts mid-build the user re-submits.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from app import backup
from app.config import file_tool_root
from app.degree_schema import ACTION_VERBS, validate_outline
from app.ollama import (
    OllamaProtocolError,
    OllamaUnavailable,
    generate_json,
)
from app.templates import templates

logger = logging.getLogger(__name__)


# Fallback model when the app has no configured default. granite4.1:8b is the
# app's primary (the Research / Content agents run on it) and handles Ollama
# structured outputs well, so the factory works out of the box without pulling
# a separate model. The routes prefer the user's configured default model
# (queries.get_default_model) and only fall back to this constant.
DEFAULT_MODEL = "granite4.1:8b"

# Sane per-course length bounds — clamps a model that proposes a 1-week course
# or a 200-week one. Capstone included (a 4-week capstone still tiles into
# Proposal/Execution/Submission).
_MIN_COURSE_WEEKS = 4
_MAX_COURSE_WEEKS = 30

_TIERS = ("intro", "intermediate", "advanced", "frontier")

# Per-stage retry budget when the model's *content* (not shape) fails a server
# gate — e.g. an outcome that doesn't start with an action verb. The offending
# items are fed back; we never retry blindly.
_MAX_RETRIES = 2

_VERB_LIST = ", ".join(sorted(ACTION_VERBS))


class DegreeFactoryError(Exception):
    """A build step failed in a way the user must see (e.g. retries exhausted,
    or the workspace is unconfigured). Carries a human-readable message."""


# ---------------------------------------------------------------------------
# Terse rule snippets — inlined (NOT read from the big template files, which is
# what bloated the old agent). Mirror the rules in DEGREE_ARCHITECT_PROMPT.
# ---------------------------------------------------------------------------

_RULES_COMMON = (
    "You design self-study curricula. Reply with ONLY valid JSON in the "
    "requested shape — no prose, no markdown fences.\n"
    "Every outcome phrase MUST start with exactly one of these action verbs "
    f"(first word only): {_VERB_LIST}. Never begin an outcome with "
    "'Understand', 'Learn', or 'Know'.\n"
    "Slugs are lowercase, underscore-separated, start with a letter, 1-3 words "
    "(e.g. 'vector_calculus')."
)

_RULES_THEMES = (
    "A theme is a recurring TENSION or organizing question that spans several "
    "courses — not a topic. Wrong: 'Conservation laws'. Right: 'Conservation as "
    "the primary problem-solving move across mechanical, thermal, and quantum "
    "domains'. Each theme is a single sentence naming a tension."
)


# ---------------------------------------------------------------------------
# Form + state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DegreeForm:
    """The minimal degree-level input collected by the ``/degrees`` form.

    Attributes:
        subject: Exact scope of the degree (one narrow subject).
        learner: Learner profile — background, prior knowledge, time budget.
        tier_benchmark: A published book the degree should make readable.
        capstone: The artifact the learner produces at the end.
        course_count: Number of courses — exactly 4, 5, or 6.
    """

    subject: str
    learner: str
    tier_benchmark: str
    capstone: str
    course_count: int


@dataclass
class DegreeDraft:
    """Stage-A result awaiting the checkpoint approval.

    Holds the form (for regenerate) plus the degree metadata and the tiled
    course list (``n`` / ``weeks_start`` / ``weeks_end`` / ``is_capstone``
    already assigned by :func:`tile_courses`). Courses still carry the scratch
    ``week_count`` the build needs; it is stripped before validation.

    ``built_courses`` accumulates the cleaned, *approved* course dicts as the
    user steps through the per-course build loop (one at a time, or in a final
    "build all remaining" burst). The next course to build is always
    ``courses[len(built_courses)]``; the final outline is assembled from
    ``built_courses`` once it holds every course.
    """

    draft_id: str
    form: DegreeForm
    degree_meta: dict
    courses: list[dict]
    built_courses: list[dict] = field(default_factory=list)


@dataclass
class DegreeJob:
    """A running build. Producer/consumer shape mirrors
    :class:`app.generation.GenerationState`.

    Attributes:
        job_id: Stable id used in the SSE URL.
        degree_slug: The slug being built (for status text).
        events: Append-only ``(event, html)`` log; consumers replay from 0.
        done: Set once the producer finishes (any exit path).
        cond: Wakes consumers on each append and on completion.
        task: The owning :class:`asyncio.Task` (owned by the registry, not the
            request).
        result_path: Absolute path of the written outline on success
            (set by a full / "build all remaining" job, not a single-course one).
        error: Human-readable failure reason on the error path.
        built_course: For a single-course job, the cleaned course dict awaiting
            the user's approve/regenerate decision. ``None`` for full builds.
        course_index: Which course index a single-course job built.
        approved: Set once the user approves this job's ``built_course`` (guards
            against a double-submit appending it twice).
    """

    job_id: str
    degree_slug: str
    events: list[tuple[str, str]] = field(default_factory=list)
    done: bool = False
    cond: asyncio.Condition = field(default_factory=asyncio.Condition)
    task: asyncio.Task | None = None
    result_path: str | None = None
    error: str | None = None
    built_course: dict | None = None
    course_index: int | None = None
    approved: bool = False


# Process-global registries — single-process app, same pattern as
# app.generation.live_generations. Not evicted on done: a reloaded page must be
# able to replay a finished job's terminal event.
degree_drafts: dict[str, DegreeDraft] = {}
degree_jobs: dict[str, DegreeJob] = {}


# ---------------------------------------------------------------------------
# Slug + value normalization (server-owned — never trust model slugs)
# ---------------------------------------------------------------------------


def _slugify(text: str, *, fallback: str) -> str:
    """Return a slug matching ``^[a-z][a-z0-9_]{0,40}$``.

    Lowercases, collapses non-alphanumerics to single underscores, and ensures
    a leading letter (prefixing ``fallback`` when needed). Capped at 41 chars.
    """
    s = re.sub(r"[^a-z0-9]+", "_", (text or "").strip().lower())
    s = re.sub(r"_+", "_", s).strip("_")
    if not s or not s[0].isalpha():
        s = f"{fallback}_{s}".strip("_")
    if not s or not s[0].isalpha():
        s = fallback
    return s[:41]


def _assign_unique_slugs(items: list[dict], *, fallback: str) -> None:
    """Slugify each item's ``slug`` (or ``title``) and de-duplicate, in place.

    Collisions get a ``_2`` / ``_3`` suffix. Satisfies the schema's
    per-scope slug-uniqueness validators without any model cooperation.
    """
    seen: set[str] = set()
    for it in items:
        base = _slugify(it.get("slug") or it.get("title", ""), fallback=fallback)
        slug = base
        k = 2
        while slug in seen:
            slug = f"{base[:38]}_{k}"
            k += 1
        seen.add(slug)
        it["slug"] = slug


def _normalize_tier(tier: object) -> str:
    """Coerce an arbitrary model value to a valid tier (default intermediate)."""
    return tier if tier in _TIERS else "intermediate"


def _nonempty(value: object, default: str) -> str:
    """Return a stripped non-empty string, or ``default`` — schema fields like
    ``title`` / ``prerequisites`` / ``focus`` require ``min_length=1``."""
    s = str(value).strip() if value is not None else ""
    return s or default


def _starts_with_verb(phrase: str) -> bool:
    """First token (sans trailing comma) is an allowed action verb.

    Mirrors ``app.degree_schema._starts_with_action_verb`` — reimplemented
    locally to avoid importing a private symbol.
    """
    if not phrase or not phrase.strip():
        return False
    return phrase.split()[0].rstrip(",") in ACTION_VERBS


# ---------------------------------------------------------------------------
# Deterministic scaffolding — must satisfy validate_dense_week_numbering
# ---------------------------------------------------------------------------


def tile_courses(courses: list[dict]) -> None:
    """Assign ``n``, dense ``weeks_start``/``weeks_end``, and ``is_capstone``.

    Course 1 starts at global week 1; course k starts where k-1 ended + 1
    (the exact rule ``validate_dense_week_numbering`` checks). Width comes from
    each course's scratch ``week_count`` (clamped ≥1). The final course is the
    capstone; all earlier ones are not. Mutates ``courses`` in place.
    """
    w = 1
    last = len(courses)
    for i, c in enumerate(courses, start=1):
        wc = max(1, int(c.get("week_count", 1)))
        c["week_count"] = wc
        c["n"] = i
        c["weeks_start"] = w
        c["weeks_end"] = w + wc - 1
        c["is_capstone"] = i == last
        w = c["weeks_end"] + 1


def reconcile_unit_week_counts(course_span: int, units: list[dict]) -> None:
    """Force ``sum(u['week_count']) == course_span`` with each unit ≥1, in place.

    The model's proposed per-unit week counts won't tile the course exactly, so
    we normalize: clamp each to ≥1, then add or remove single weeks round-robin
    until the totals match. This is the linchpin that makes the deterministic
    guarantee hold regardless of the model's arithmetic.

    Args:
        course_span: ``weeks_end - weeks_start + 1`` for the course.
        units: The course's unit dicts (mutated).

    Raises:
        DegreeFactoryError: when ``course_span < len(units)`` — there aren't
            enough weeks to give every unit one. The caller trims units first,
            so this is a defensive guard.
    """
    n = len(units)
    if n == 0:
        return
    if course_span < n:
        raise DegreeFactoryError(
            f"course span {course_span} cannot hold {n} units"
        )
    for u in units:
        u["week_count"] = max(1, int(u.get("week_count", 1)))
    diff = course_span - sum(u["week_count"] for u in units)
    i = 0
    while diff > 0:  # hand out extra weeks round-robin
        units[i % n]["week_count"] += 1
        diff -= 1
        i += 1
    while diff < 0:  # reclaim weeks from any unit with more than one
        u = units[i % n]
        if u["week_count"] > 1:
            u["week_count"] -= 1
            diff += 1
        i += 1


def tile_units(course_start: int, units: list[dict]) -> None:
    """Assign ``n`` and dense ``weeks_start``/``weeks_end`` to a course's units.

    Each unit tiles the course span from ``course_start``. Assumes
    :func:`reconcile_unit_week_counts` already made the widths sum to the span.
    Mutates ``units`` in place.
    """
    w = course_start
    for i, u in enumerate(units, start=1):
        wc = max(1, int(u.get("week_count", 1)))
        u["n"] = i
        u["weeks_start"] = w
        u["weeks_end"] = w + wc - 1
        w = u["weeks_end"] + 1


def assign_week_numbers(unit_start: int, weeks: list[dict]) -> None:
    """Set each week's global ``n`` densely from ``unit_start`` (the value
    ``validate_dense_week_numbering`` requires). Mutates ``weeks`` in place."""
    for i, wk in enumerate(weeks):
        wk["n"] = unit_start + i


# Three generic, verb-gated outcomes for a synthesized backstop week.
_FILLER_OUTCOMES = (
    "Apply the unit's key concepts to a consolidation exercise.",
    "Analyze how this week's material connects to the unit's focus.",
    "Explain the central ideas of the unit in your own words.",
)


def _synth_week(unit: dict) -> dict:
    """Build one schema-valid standard week as a backstop.

    Used only when the model returned fewer weeks than the unit needs. Pulls 6
    key-term names from the unit's own glossary (standard units always have 12).
    Slug is finalized by :func:`_assign_unique_slugs`.
    """
    glossary = list(unit.get("glossary_terms") or unit.get("key_concepts") or [])
    key_terms = (glossary + ["review"] * 6)[:6]
    return {
        "slug": "review",
        "title": "Consolidation and review",
        "focus": f"Consolidate the material in {unit.get('title', 'this unit')}.",
        "outcome_phrases": list(_FILLER_OUTCOMES),
        "key_term_names": key_terms,
    }


def _fit_week_objects(unit: dict, weeks: list[dict]) -> list[dict]:
    """Return exactly ``weeks_end - weeks_start + 1`` week dicts.

    Truncates extras; pads short counts with :func:`_synth_week`. The final
    backstop ensuring a unit's week objects tile its span exactly.
    """
    target = unit["weeks_end"] - unit["weeks_start"] + 1
    out = list(weeks)[:target]
    while len(out) < target:
        out.append(_synth_week(unit))
    return out


def _phase_sequence(span: int) -> list[str]:
    """Distribute ``span`` capstone weeks across the three phases, in order.

    For ``span >= 3`` every phase appears; tiny spans degrade sensibly. The
    schema only requires each capstone week to carry *a* phase, not that all
    three appear, so this is a presentation choice.
    """
    span = max(1, span)
    if span == 1:
        return ["Execution"]
    if span == 2:
        return ["Proposal", "Submission"]
    n_prop = max(1, span // 4)
    n_sub = max(1, span // 4)
    n_exec = span - n_prop - n_sub  # ≥1 for span ≥ 3
    return ["Proposal"] * n_prop + ["Execution"] * n_exec + ["Submission"] * n_sub


# ---------------------------------------------------------------------------
# Content gates — semantic rules Ollama's `format` can't enforce. Return a list
# of human-readable problems (empty == clean); the messages are fed back to the
# model on retry.
# ---------------------------------------------------------------------------


def content_errors_stage_a(d: dict, course_count: int) -> list[str]:
    """Verb-gate the program outcomes and count themes / outcomes / courses."""
    errs: list[str] = []
    themes = d.get("themes") or []
    if len(themes) != 4:
        errs.append(f"need exactly 4 themes, got {len(themes)}")
    outs = d.get("program_outcome_phrases") or []
    if len(outs) != 6:
        errs.append(f"need exactly 6 program_outcome_phrases, got {len(outs)}")
    bad = [p for p in outs if not _starts_with_verb(p)]
    if bad:
        errs.append(
            f"these program outcomes must start with an action verb "
            f"({_VERB_LIST}): {bad}"
        )
    courses = d.get("courses") or []
    if len(courses) != course_count:
        errs.append(f"need exactly {course_count} courses, got {len(courses)}")
    return errs


def content_errors_units(units: list[dict]) -> list[str]:
    """Check each unit's counts + verb gate, and glossary uniqueness across the
    course (the whole course's units arrive in one call)."""
    errs: list[str] = []
    if not units:
        errs.append("need at least one unit")
    seen_term_unit: dict[str, str] = {}
    for u in units:
        where = u.get("title") or u.get("slug") or "unit"
        outs = u.get("outcome_phrases") or []
        if len(outs) != 6:
            errs.append(f"unit '{where}': need exactly 6 outcome_phrases, got {len(outs)}")
        bad = [p for p in outs if not _starts_with_verb(p)]
        if bad:
            errs.append(f"unit '{where}': these outcomes need an action verb: {bad}")
        if len(u.get("key_concepts") or []) != 8:
            errs.append(f"unit '{where}': need exactly 8 key_concepts")
        gl = u.get("glossary_terms") or []
        if len(gl) != 12:
            errs.append(f"unit '{where}': need exactly 12 glossary_terms, got {len(gl)}")
        low = [str(t).strip().lower() for t in gl]
        if len(set(low)) != len(low):
            errs.append(f"unit '{where}': glossary_terms must be unique within the unit")
        for t in set(low):
            if t in seen_term_unit and seen_term_unit[t] != where:
                errs.append(
                    f"glossary term '{t}' appears in more than one unit — "
                    "terms must be unique across the whole course"
                )
            seen_term_unit.setdefault(t, where)
    return errs


def content_errors_weeks(weeks: list[dict]) -> list[str]:
    """Per-week content gate (counts + verb gate). Array length is NOT checked
    here — :func:`_fit_week_objects` owns the count, so a slightly-off count
    shouldn't burn a retry."""
    errs: list[str] = []
    for w in weeks:
        where = w.get("title") or w.get("slug") or "week"
        outs = w.get("outcome_phrases") or []
        if not 3 <= len(outs) <= 4:
            errs.append(f"week '{where}': need 3 or 4 outcome_phrases, got {len(outs)}")
        bad = [p for p in outs if not _starts_with_verb(p)]
        if bad:
            errs.append(f"week '{where}': these outcomes need an action verb: {bad}")
        if not 6 <= len(w.get("key_term_names") or []) <= 8:
            errs.append(f"week '{where}': need 6 to 8 key_term_names")
    return errs


# ---------------------------------------------------------------------------
# Per-stage format schemas — hand-written FLAT JSON Schema (no $ref/$defs, which
# Ollama's structured-output compiler rejects).
# ---------------------------------------------------------------------------

_TIER_SCHEMA = {"type": "string", "enum": list(_TIERS)}
_STR = {"type": "string"}
_STR_ARRAY = {"type": "array", "items": _STR}


def _exact_str_array(n: int) -> dict:
    return {"type": "array", "items": _STR, "minItems": n, "maxItems": n}


def _range_str_array(lo: int, hi: int) -> dict:
    return {"type": "array", "items": _STR, "minItems": lo, "maxItems": hi}


def _stage_a_schema(course_count: int) -> dict:
    return {
        "type": "object",
        "properties": {
            "slug": _STR,
            "title": _STR,
            "tier_reached": _TIER_SCHEMA,
            "prerequisites": _STR,
            "themes": _exact_str_array(4),
            "program_outcome_phrases": _exact_str_array(6),
            "courses": {
                "type": "array",
                "minItems": course_count,
                "maxItems": course_count,
                "items": {
                    "type": "object",
                    "properties": {
                        "slug": _STR,
                        "title": _STR,
                        "focus": _STR,
                        "tier": _TIER_SCHEMA,
                        "key_capability": _STR,
                        "week_count": {"type": "integer"},
                    },
                    "required": ["slug", "title", "focus", "tier", "week_count"],
                },
            },
        },
        "required": [
            "slug", "title", "tier_reached", "prerequisites",
            "themes", "program_outcome_phrases", "courses",
        ],
    }


def _stage_b_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "units": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "slug": _STR,
                        "title": _STR,
                        "focus": _STR,
                        "week_count": {"type": "integer"},
                        "outcome_phrases": _exact_str_array(6),
                        "key_concepts": _exact_str_array(8),
                        "glossary_terms": _exact_str_array(12),
                    },
                    "required": [
                        "slug", "title", "week_count",
                        "outcome_phrases", "key_concepts", "glossary_terms",
                    ],
                },
            }
        },
        "required": ["units"],
    }


def _stage_c_schema(target: int) -> dict:
    return {
        "type": "object",
        "properties": {
            "weeks": {
                "type": "array",
                "minItems": target,
                "maxItems": target,
                "items": {
                    "type": "object",
                    "properties": {
                        "slug": _STR,
                        "title": _STR,
                        "focus": _STR,
                        "outcome_phrases": _range_str_array(3, 4),
                        "key_term_names": _range_str_array(6, 8),
                    },
                    "required": ["slug", "title", "outcome_phrases", "key_term_names"],
                },
            }
        },
        "required": ["weeks"],
    }


def _capstone_schema(span: int) -> dict:
    return {
        "type": "object",
        "properties": {
            "title": _STR,
            "focus": _STR,
            "unit_title": _STR,
            "week_titles": _exact_str_array(span),
        },
        "required": ["title", "focus", "unit_title", "week_titles"],
    }


# ---------------------------------------------------------------------------
# Per-stage prompt builders
# ---------------------------------------------------------------------------


def _stage_a_user(form: DegreeForm) -> str:
    return (
        "Design the top level of a self-study degree.\n\n"
        f"Subject / exact scope: {form.subject}\n"
        f"Learner profile: {form.learner}\n"
        f"Tier benchmark (a book it should make readable by the end): "
        f"{form.tier_benchmark}\n"
        f"Capstone artifact the learner produces: {form.capstone}\n\n"
        "Produce JSON with: slug, title, tier_reached, prerequisites (one line), "
        "themes (EXACTLY 4 tensions), program_outcome_phrases (EXACTLY 6, each "
        "starting with an action verb and each needing content from at least two "
        f"courses), and courses (EXACTLY {form.course_count}).\n"
        "Each course needs: slug, title, focus (one line), tier, key_capability "
        "(one line), and week_count (integer). Most courses run 8-20 weeks; the "
        "FINAL course is the capstone project course and is shorter (~4-8 weeks).\n"
        "Do NOT number weeks, set is_capstone, or add totals — the system "
        "computes all structure."
    )


def _stage_b_user(course: dict, themes: list[str]) -> str:
    span = course["weeks_end"] - course["weeks_start"] + 1
    suggested = max(1, min(span, round(span / 4) or 1))
    return (
        "Break ONE course of a self-study degree into units.\n\n"
        f"Course: {course['title']}\n"
        f"Focus: {course.get('focus', '')}\n"
        f"Tier: {course.get('tier', '')}\n"
        f"This course runs {span} weeks total.\n"
        f"Degree themes it should exercise: {'; '.join(themes)}\n\n"
        f"Propose about {suggested} units (aim for ~3-5 weeks each). For EACH "
        "unit produce: slug, title, focus (one line), week_count (integer >=1), "
        "outcome_phrases (EXACTLY 6, verb-gated), key_concepts (EXACTLY 8), and "
        "glossary_terms (EXACTLY 12 term names). A glossary term must NOT repeat "
        "in another unit of this course.\n"
        f"The unit week_counts should roughly sum to {span}."
    )


def _stage_c_user(unit: dict, target: int) -> str:
    return (
        "Break ONE unit of a course into its weeks.\n\n"
        f"Unit: {unit['title']}\n"
        f"Focus: {unit.get('focus', '')}\n"
        f"Key concepts: {', '.join(unit.get('key_concepts') or [])}\n"
        f"Glossary available to draw on: {', '.join(unit.get('glossary_terms') or [])}\n\n"
        f"Produce EXACTLY {target} weeks. For EACH week: slug, title, focus "
        "(one line), outcome_phrases (3 or 4, verb-gated), and key_term_names "
        "(6 to 8 term names — names only)."
    )


def _capstone_user(course: dict, span: int) -> str:
    return (
        "Design the capstone course of a self-study degree — the final, "
        "project-based course.\n\n"
        f"Working title: {course.get('title', 'Capstone')}\n"
        f"Focus: {course.get('focus', '')}\n"
        f"It runs {span} weeks across three phases (Proposal, then Execution, "
        "then Submission).\n\n"
        "Produce JSON with: title, focus (one line), unit_title, and week_titles "
        f"(EXACTLY {span} short titles describing that week's project work, in "
        "order from proposal through submission)."
    )


# ---------------------------------------------------------------------------
# Generation with bounded, error-feeding retries
# ---------------------------------------------------------------------------


async def _generate_checked(
    client,
    *,
    model: str,
    system: str,
    user: str,
    schema: dict,
    num_ctx: int | None,
    gate: Callable[[dict], list[str]],
) -> dict:
    """Call :func:`generate_json`, run ``gate``, and retry up to ``_MAX_RETRIES``
    feeding the gate's complaints back. Raises :class:`DegreeFactoryError` when
    the budget is exhausted (never retries blindly)."""
    note = ""
    last: list[str] = []
    for _ in range(_MAX_RETRIES + 1):
        prompt = user if not note else (
            f"{user}\n\nYour previous attempt had these problems — fix them:\n{note}"
        )
        data = await generate_json(
            client, model, system=system, user=prompt,
            format_schema=schema, num_ctx=num_ctx,
        )
        last = gate(data)
        if not last:
            return data
        note = "\n".join(f"- {e}" for e in last)
    raise DegreeFactoryError(
        "The model could not satisfy the requirements after retries: "
        + "; ".join(last)
    )


# ---------------------------------------------------------------------------
# Stage A — degree skeleton + checkpoint draft
# ---------------------------------------------------------------------------


async def _run_stage_a(
    client, form: DegreeForm, *, model: str, num_ctx: int | None
) -> tuple[dict, list[dict]]:
    """Generate + normalize the degree metadata and course list (pre-tiling)."""
    data = await _generate_checked(
        client, model=model,
        system=f"{_RULES_COMMON}\n{_RULES_THEMES}",
        user=_stage_a_user(form),
        schema=_stage_a_schema(form.course_count),
        num_ctx=num_ctx,
        gate=lambda d: content_errors_stage_a(d, form.course_count),
    )
    degree_meta = {
        "slug": _slugify(data.get("slug") or data.get("title", ""), fallback="degree"),
        "title": _nonempty(data.get("title"), "Self-study degree"),
        "tier_reached": _normalize_tier(data.get("tier_reached")),
        "prerequisites": _nonempty(data.get("prerequisites"), "None stated."),
        "themes": [_nonempty(t, "Theme") for t in (data.get("themes") or [])],
        "program_outcome_phrases": list(data.get("program_outcome_phrases") or []),
    }
    courses = []
    for c in data.get("courses") or []:
        courses.append({
            "slug": c.get("slug") or c.get("title", ""),
            "title": _nonempty(c.get("title"), "Course"),
            "focus": _nonempty(c.get("focus"), "Course focus."),
            "tier": _normalize_tier(c.get("tier")),
            "key_capability": _nonempty(c.get("key_capability"), "")
            or None,
            "week_count": c.get("week_count", 12),
        })
    _assign_unique_slugs(courses, fallback="course")
    return degree_meta, courses


def _normalize_course_week_counts(courses: list[dict]) -> None:
    """Clamp each course's ``week_count`` into ``[_MIN, _MAX]`` (in place)."""
    for c in courses:
        try:
            wc = int(c.get("week_count", 12))
        except (TypeError, ValueError):
            wc = 12
        c["week_count"] = max(_MIN_COURSE_WEEKS, min(_MAX_COURSE_WEEKS, wc))


def _store_draft(form: DegreeForm, degree_meta: dict, courses: list[dict]) -> DegreeDraft:
    """Normalize + tile the courses and register a new :class:`DegreeDraft`."""
    _normalize_course_week_counts(courses)
    tile_courses(courses)
    draft = DegreeDraft(
        draft_id=uuid4().hex,
        form=form,
        degree_meta=degree_meta,
        courses=courses,
    )
    degree_drafts[draft.draft_id] = draft
    return draft


async def create_draft(
    client, form: DegreeForm, *, model: str = DEFAULT_MODEL, num_ctx: int | None = None
) -> DegreeDraft:
    """Run Stage A and register a fresh checkpoint draft."""
    degree_meta, courses = await _run_stage_a(
        client, form, model=model, num_ctx=num_ctx
    )
    return _store_draft(form, degree_meta, courses)


async def regenerate_draft(
    client, draft: DegreeDraft, *, note: str = "",
    model: str = DEFAULT_MODEL, num_ctx: int | None = None,
) -> DegreeDraft:
    """Re-run Stage A for an existing draft (optionally nudged by ``note``),
    overwriting its degree metadata + course list in place."""
    form = draft.form
    user = _stage_a_user(form)
    if note.strip():
        user += f"\n\nThe user wants this changed from the last version: {note.strip()}"
    data = await _generate_checked(
        client, model=model,
        system=f"{_RULES_COMMON}\n{_RULES_THEMES}",
        user=user,
        schema=_stage_a_schema(form.course_count),
        num_ctx=num_ctx,
        gate=lambda d: content_errors_stage_a(d, form.course_count),
    )
    degree_meta = {
        "slug": _slugify(data.get("slug") or data.get("title", ""), fallback="degree"),
        "title": _nonempty(data.get("title"), "Self-study degree"),
        "tier_reached": _normalize_tier(data.get("tier_reached")),
        "prerequisites": _nonempty(data.get("prerequisites"), "None stated."),
        "themes": [_nonempty(t, "Theme") for t in (data.get("themes") or [])],
        "program_outcome_phrases": list(data.get("program_outcome_phrases") or []),
    }
    courses = []
    for c in data.get("courses") or []:
        courses.append({
            "slug": c.get("slug") or c.get("title", ""),
            "title": _nonempty(c.get("title"), "Course"),
            "focus": _nonempty(c.get("focus"), "Course focus."),
            "tier": _normalize_tier(c.get("tier")),
            "key_capability": _nonempty(c.get("key_capability"), "") or None,
            "week_count": c.get("week_count", 12),
        })
    _assign_unique_slugs(courses, fallback="course")
    _normalize_course_week_counts(courses)
    tile_courses(courses)
    draft.degree_meta = degree_meta
    draft.courses = courses
    return draft


# ---------------------------------------------------------------------------
# Field whitelists — strip scratch keys (week_count) before validation, since
# every schema model is ConfigDict(extra="forbid").
# ---------------------------------------------------------------------------

_DEGREE_FIELDS = (
    "slug", "title", "tier_reached", "prerequisites",
    "themes", "program_outcome_phrases",
)
_COURSE_FIELDS = (
    "n", "slug", "title", "focus", "tier", "weeks_start", "weeks_end",
    "key_capability", "is_capstone", "themes_emphasized", "units",
)
_UNIT_FIELDS = (
    "n", "slug", "title", "focus", "weeks_start", "weeks_end", "is_capstone",
    "outcome_phrases", "key_concepts", "glossary_terms", "weeks",
)
_WEEK_FIELDS = (
    "n", "slug", "title", "focus", "outcome_phrases", "key_term_names", "phase",
)


def _pick(d: dict, fields: tuple[str, ...]) -> dict:
    """Whitelist ``fields`` present in ``d`` (drops scratch + stray keys)."""
    return {k: d[k] for k in fields if k in d}


# ---------------------------------------------------------------------------
# Stage B / C — fill one course's units, then each unit's weeks
# ---------------------------------------------------------------------------


async def _build_course(
    client, course: dict, themes: list[str], *, model: str, num_ctx: int | None,
    on_progress: Callable[[str], "asyncio.Future | None"] | None = None,
) -> dict:
    """Fill a non-capstone course: Stage B units, then Stage C weeks per unit.

    Returns the cleaned (schema-shaped) course dict.
    """
    course_span = course["weeks_end"] - course["weeks_start"] + 1
    data = await _generate_checked(
        client, model=model, system=_RULES_COMMON,
        user=_stage_b_user(course, themes), schema=_stage_b_schema(),
        num_ctx=num_ctx, gate=lambda d: content_errors_units(d.get("units", [])),
    )
    units = data["units"][:course_span]  # can't have more units than weeks
    _assign_unique_slugs(units, fallback="unit")
    reconcile_unit_week_counts(course_span, units)
    tile_units(course["weeks_start"], units)

    for u in units:
        if on_progress is not None:
            await on_progress(f"{course['title']} — unit {u['n']}: {u['title']}")
        target = u["weeks_end"] - u["weeks_start"] + 1
        wdata = await _generate_checked(
            client, model=model, system=_RULES_COMMON,
            user=_stage_c_user(u, target), schema=_stage_c_schema(target),
            num_ctx=num_ctx, gate=lambda d: content_errors_weeks(d.get("weeks", [])),
        )
        weeks = _fit_week_objects(u, wdata["weeks"])
        _assign_unique_slugs(weeks, fallback="week")
        assign_week_numbers(u["weeks_start"], weeks)
        u["weeks"] = [_pick(w, _WEEK_FIELDS) for w in weeks]

    course["units"] = [_pick(u, _UNIT_FIELDS) for u in units]
    return _pick(course, _COURSE_FIELDS)


async def _build_capstone(
    client, course: dict, *, model: str, num_ctx: int | None
) -> dict:
    """Build the final (capstone) course: ≤1 model call for titles, all
    structure (unit, phase weeks, numbering) computed server-side."""
    span = course["weeks_end"] - course["weeks_start"] + 1
    data: dict = {}
    try:
        data = await generate_json(
            client, model, system=_RULES_COMMON,
            user=_capstone_user(course, span),
            format_schema=_capstone_schema(span), num_ctx=num_ctx,
        )
    except (OllamaUnavailable, OllamaProtocolError):
        # Titles are cosmetic for the capstone; fall back to deterministic ones
        # rather than failing the whole build over the last course.
        logger.warning("capstone title generation failed; using fallbacks")

    phases = _phase_sequence(span)
    titles = data.get("week_titles") or []
    weeks = []
    for i in range(span):
        wt = titles[i].strip() if i < len(titles) and str(titles[i]).strip() else ""
        weeks.append({
            "title": wt or f"{phases[i]}: week {course['weeks_start'] + i}",
            "phase": phases[i],
            "slug": phases[i].lower(),
        })
    _assign_unique_slugs(weeks, fallback="week")
    assign_week_numbers(course["weeks_start"], weeks)

    unit = {
        "n": 1,
        "slug": "capstone",
        "title": _nonempty(data.get("unit_title"), "Capstone project"),
        "weeks_start": course["weeks_start"],
        "weeks_end": course["weeks_end"],
        "is_capstone": True,
        "weeks": [_pick(w, _WEEK_FIELDS) for w in weeks],
    }
    out = {
        "n": course["n"],
        "slug": course["slug"],
        "title": _nonempty(data.get("title"), course.get("title")),
        "focus": _nonempty(data.get("focus"), course.get("focus")),
        "tier": _normalize_tier(course.get("tier")),
        "weeks_start": course["weeks_start"],
        "weeks_end": course["weeks_end"],
        "key_capability": course.get("key_capability"),
        "is_capstone": True,
        "units": [unit],
    }
    return _pick(out, _COURSE_FIELDS)


def _write_outline(slug: str, outline: dict) -> Path:
    """Write ``<FILE_TOOL_ROOT>/<slug>/degree_outline.json`` and return its path.

    Raises:
        DegreeFactoryError: when the workspace is unconfigured or the slug
            would escape it (defensive — slugs are sanitized upstream).
    """
    root = file_tool_root()
    if root is None:
        raise DegreeFactoryError("Workspace not configured (FILE_TOOL_ROOT unset).")
    target_dir = (root / slug).resolve()
    if not target_dir.is_relative_to(root):
        raise DegreeFactoryError("Invalid degree slug path.")
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / "degree_outline.json"
    path.write_text(
        json.dumps(outline, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return path


# ---------------------------------------------------------------------------
# Build orchestrator (the producer task) + SSE plumbing
# ---------------------------------------------------------------------------


def _sse(payload: str, event: str | None = None) -> str:
    """Format an HTML payload as one SSE message (one ``data:`` line per
    newline). Copied from :mod:`app.generation` to stay decoupled."""
    prefix = f"event: {event}\n" if event else ""
    lines = payload.split("\n") if payload else [""]
    data_lines = "".join(f"data: {line}\n" for line in lines)
    return f"{prefix}{data_lines}\n"


async def _emit(job: DegreeJob, event: str, payload: str) -> None:
    """Append one SSE event and wake all consumers (atomic under ``cond``)."""
    async with job.cond:
        job.events.append((event, payload))
        job.cond.notify_all()


async def _signal_done(job: DegreeJob) -> None:
    """Mark the job done and wake every consumer (producer's last act)."""
    async with job.cond:
        job.done = True
        job.cond.notify_all()


def _progress_html(text: str) -> str:
    return (
        '<div class="degree-progress__status">'
        '<span class="degree-progress__spinner" aria-hidden="true"></span>'
        f"<span>{html.escape(text)}</span></div>"
    )


def _done_html(job_id: str, slug: str, path: Path) -> str:
    # OOB-replaces the whole sse-connected container (removing it stops the
    # browser's EventSource from reconnecting — same trick as the chat done).
    return (
        f'<div id="degree-build-{html.escape(job_id)}" '
        'class="degree-progress degree-progress--done" '
        f'hx-swap-oob="outerHTML:#degree-build-{html.escape(job_id)}">'
        f'<p class="degree-progress__success">Built <strong>{html.escape(slug)}'
        f"</strong>. Saved <code>{html.escape(str(path))}</code>.</p>"
        '<a class="degree-progress__link" href="/degrees" hx-get="/degrees" '
        'hx-target="#main" hx-swap="innerHTML" hx-push-url="/degrees">'
        "Back to Degrees</a></div>"
    )


def _error_html(job_id: str, title: str, detail: str) -> str:
    return (
        f'<div id="degree-build-{html.escape(job_id)}" '
        'class="degree-progress degree-progress--error" '
        f'hx-swap-oob="outerHTML:#degree-build-{html.escape(job_id)}">'
        f'<p class="degree-progress__error-title">{html.escape(title)}</p>'
        f"<pre class=\"degree-progress__error\">{html.escape(detail)}</pre>"
        '<a class="degree-progress__link" href="/degrees" hx-get="/degrees" '
        'hx-target="#main" hx-swap="innerHTML" hx-push-url="/degrees">'
        "Back to Degrees</a></div>"
    )


def _finished_html(job_id: str) -> str:
    return (
        f'<div id="degree-build-{html.escape(job_id)}" class="degree-progress" '
        f'hx-swap-oob="outerHTML:#degree-build-{html.escape(job_id)}">'
        "<p>This build is no longer being tracked (the server may have "
        "restarted). Check the list below for the finished outline.</p>"
        '<a class="degree-progress__link" href="/degrees" hx-get="/degrees" '
        'hx-target="#main" hx-swap="innerHTML" hx-push-url="/degrees">'
        "Back to Degrees</a></div>"
    )


async def _build_one(
    client, course: dict, themes: list[str], *,
    model: str, num_ctx: int | None, on_progress=None,
) -> dict:
    """Build one cleaned course — dispatches capstone vs standard."""
    if course["is_capstone"]:
        return await _build_capstone(client, course, model=model, num_ctx=num_ctx)
    return await _build_course(
        client, course, themes, model=model, num_ctx=num_ctx, on_progress=on_progress
    )


def assemble_and_write(draft: DegreeDraft) -> Path:
    """Assemble the approved courses into the outline, validate, and write it.

    Args:
        draft: A draft whose ``built_courses`` holds every cleaned course.

    Returns:
        The path of the written ``degree_outline.json``.

    Raises:
        pydantic.ValidationError: the assembled outline failed the schema.
        DegreeFactoryError: the workspace is unconfigured / slug escapes it.
    """
    outline = {
        "version": 1,
        "degree": {
            **_pick(draft.degree_meta, _DEGREE_FIELDS),
            "total_courses": len(draft.built_courses),
        },
        "courses": list(draft.built_courses),
    }
    validate_outline(outline)
    path = _write_outline(draft.degree_meta["slug"], outline)
    try:
        backup.request_backup("manual")
    except Exception:  # backup is best-effort; never fail a build over it
        logger.warning("degree outline backup request failed", exc_info=True)
    return path


def _course_review_html(
    job: DegreeJob, draft: DegreeDraft, built: dict, idx: int
) -> str:
    """Render the per-course review fragment (units + week titles + controls),
    delivered as a single-course job's terminal ``done`` event."""
    total = len(draft.courses)
    return templates.get_template("_degree_course_review.html").render(
        job_id=job.job_id,
        draft_id=draft.draft_id,
        course=built,
        number=idx + 1,
        total=total,
        is_last=(idx == total - 1),
    )


async def run_build(
    job: DegreeJob, *, client, draft: DegreeDraft,
    model: str = DEFAULT_MODEL, num_ctx: int | None = None,
) -> None:
    """Producer: build every *remaining* course, assemble, validate, write.

    Builds ``courses[len(built_courses):]`` — so it serves both a full build
    from an empty draft and the 'build all remaining' tail after some courses
    were approved one at a time. Never raises out; every exit path signals done
    so consumers terminate cleanly."""
    n = len(draft.courses)

    async def progress(text: str) -> None:
        await _emit(job, "progress", _progress_html(text))

    try:
        for idx in range(len(draft.built_courses), n):
            c = draft.courses[idx]
            await progress(f"Building course {idx + 1}/{n}: {c['title']}")
            draft.built_courses.append(
                await _build_one(
                    client, c, draft.degree_meta["themes"],
                    model=model, num_ctx=num_ctx, on_progress=progress,
                )
            )
        await progress("Validating and writing the outline…")
        path = assemble_and_write(draft)
        job.result_path = str(path)
        await _emit(job, "done", _done_html(job.job_id, draft.degree_meta["slug"], path))
    except DegreeFactoryError as e:
        job.error = str(e)
        await _emit(job, "error", _error_html(job.job_id, "Couldn't build the outline", str(e)))
    except (OllamaUnavailable, OllamaProtocolError) as e:
        job.error = str(e)
        await _emit(job, "error", _error_html(job.job_id, "Ollama failed during the build", str(e)))
    except Exception as e:  # defensive: ValidationError + anything unexpected
        job.error = str(e)
        await _emit(job, "error", _error_html(job.job_id, "The outline failed to build", str(e)))
        logger.exception("degree build %s failed", job.job_id)
    finally:
        await _signal_done(job)


async def run_course_build(
    job: DegreeJob, *, client, draft: DegreeDraft,
    model: str = DEFAULT_MODEL, num_ctx: int | None = None,
) -> None:
    """Producer: build ONE course (the next un-approved) and emit a review
    fragment so the user can approve / regenerate before moving on. Stores the
    cleaned course on ``job.built_course``. Never raises out."""
    idx = len(draft.built_courses)
    n = len(draft.courses)

    async def progress(text: str) -> None:
        await _emit(job, "progress", _progress_html(text))

    try:
        c = draft.courses[idx]
        await progress(f"Building course {idx + 1}/{n}: {c['title']}")
        built = await _build_one(
            client, c, draft.degree_meta["themes"],
            model=model, num_ctx=num_ctx, on_progress=progress,
        )
        job.built_course = built
        job.course_index = idx
        await _emit(job, "done", _course_review_html(job, draft, built, idx))
    except DegreeFactoryError as e:
        job.error = str(e)
        await _emit(job, "error", _error_html(job.job_id, "Couldn't build the course", str(e)))
    except (OllamaUnavailable, OllamaProtocolError) as e:
        job.error = str(e)
        await _emit(job, "error", _error_html(job.job_id, "Ollama failed during the build", str(e)))
    except Exception as e:  # defensive
        job.error = str(e)
        await _emit(job, "error", _error_html(job.job_id, "The course failed to build", str(e)))
        logger.exception("degree course build %s failed", job.job_id)
    finally:
        await _signal_done(job)


def _make_done_callback(job_id: str) -> Callable[[asyncio.Task], None]:
    def _cb(task: asyncio.Task) -> None:
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:  # producers catch their own errors; this is paranoia
            logger.error("degree build %s task crashed: %r", job_id, exc)
    return _cb


def _spawn(coro_fn, *, client, draft: DegreeDraft, model: str, num_ctx: int | None) -> DegreeJob:
    """Register a job and spawn ``coro_fn(job, …)`` as its owned task.

    Registers in :data:`degree_jobs` *before* creating the task so a consumer
    that attaches immediately always finds the job (mirrors
    :func:`app.generation.start_generation`)."""
    job = DegreeJob(job_id=uuid4().hex, degree_slug=draft.degree_meta["slug"])
    degree_jobs[job.job_id] = job
    job.task = asyncio.create_task(
        coro_fn(job, client=client, draft=draft, model=model, num_ctx=num_ctx)
    )
    job.task.add_done_callback(_make_done_callback(job.job_id))
    return job


async def start_build(
    *, client, draft: DegreeDraft,
    model: str = DEFAULT_MODEL, num_ctx: int | None = None,
) -> DegreeJob:
    """Spawn a full / 'build all remaining' job (builds the rest + writes)."""
    return _spawn(run_build, client=client, draft=draft, model=model, num_ctx=num_ctx)


async def start_course_build(
    *, client, draft: DegreeDraft,
    model: str = DEFAULT_MODEL, num_ctx: int | None = None,
) -> DegreeJob:
    """Spawn a single-course job (builds the next un-approved course)."""
    return _spawn(run_course_build, client=client, draft=draft, model=model, num_ctx=num_ctx)


async def consume_degree_job(job: DegreeJob) -> AsyncIterator[str]:
    """Yield SSE events for a job, replaying from index 0 then tailing.

    A reloaded page sees every event already emitted; an early consumer
    iterates in lock-step. Mirrors :func:`app.generation.consume_generation`.
    """
    pos = 0
    while True:
        while pos < len(job.events):
            event, payload = job.events[pos]
            yield _sse(payload, event=event)
            pos += 1
        if job.done:
            return
        async with job.cond:
            if job.done or pos < len(job.events):
                continue
            await job.cond.wait()


async def consume_degree_job_finished(job_id: str) -> AsyncIterator[str]:
    """Emit one terminal ``done`` event for a job missing from the registry.

    Only reachable after a server restart (jobs aren't evicted on completion).
    Closes the spinner so a reloaded build page doesn't hang forever.
    """
    yield _sse(_finished_html(job_id), event="done")
