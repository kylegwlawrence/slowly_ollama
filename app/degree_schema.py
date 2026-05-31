"""Phase 23: Pydantic schema for the degree outline JSON.

The outline (``degree_outline.json``) is the single hand-off artifact between
the Degree Architect chat agent and the bulk-fill orchestrator
(``app/degree_factory.py``, Phase B+). It carries every structural decision —
course/unit/week titles, focus statements, outcome phrases, glossary terms —
that the bulk-fill phase relies on to keep cross-file coherence.

The schema is intentionally strict. A bad outline produces 100+ well-filled
bad files, so validation is run twice:

  * On the Architect's side, before ``write_file`` saves the JSON, so the
    user sees the parse error in chat.
  * On the factory side at ``fill`` time, so a hand-edited or stale outline
    is rejected before any LLM call.

See ``docs/plans/phase23-degree-factory.md`` for the rationale and the full
data shape; this module is the executable spec.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


# The closed list of action verbs that every Learning Outcome / Program Outcome
# must start with. Mirrors the lists in ``DEGREE_TEMPLATE.md`` etc. — changing
# this set requires updating those templates in lockstep.
ACTION_VERBS: frozenset[str] = frozenset({
    "Derive",
    "Apply",
    "Compute",
    "Compare",
    "Explain",
    "Analyze",
    "Interpret",
    "Predict",
    "Model",
    "Simulate",
    "Argue",
    "Critique",
    "Compose",
})

# Slugs are lowercase, underscore-separated, must start with a letter. Length
# capped to keep filenames sane.
SLUG_PATTERN = r"^[a-z][a-z0-9_]{0,40}$"

# Valid difficulty tiers, from intro to research-frontier.
Tier = Literal["intro", "intermediate", "advanced", "frontier"]

# Phases of a capstone week, in execution order. Mirrors the three phases
# documented in ``CAPSTONE_WEEK_TEMPLATE.md``.
CapstonePhase = Literal["Proposal", "Execution", "Submission"]


def _starts_with_action_verb(phrase: str) -> bool:
    """Return True when ``phrase``'s first token (sans trailing comma) is an
    allowed action verb.

    Tolerates compound openings like ``"Apply, compute, and compare..."`` —
    the gate is the first verb only, per ``DEGREE_TEMPLATE.md``'s rule.
    """
    if not phrase or not phrase.strip():
        return False
    first = phrase.split()[0].rstrip(",")
    return first in ACTION_VERBS


def _check_verbs(phrases: list[str], where: str) -> None:
    """Raise ``ValueError`` when any phrase in ``phrases`` fails the verb gate.

    Args:
        phrases: The outcome phrases to check.
        where: Human-readable location for the error message
            (e.g., ``"unit math_foundations.calculus_review"``).
    """
    bad = [p for p in phrases if not _starts_with_action_verb(p)]
    if bad:
        raise ValueError(
            f"{where}: outcome(s) do not start with an allowed action verb "
            f"({sorted(ACTION_VERBS)}); offending: {bad!r}"
        )


# ---------------------------------------------------------------------------
# Leaf: Week
# ---------------------------------------------------------------------------


class Week(BaseModel):
    """One week in the curriculum, either standard or capstone-phase.

    Standard weeks have ``outcome_phrases`` + ``key_term_names`` and no
    ``phase``. Capstone weeks have ``phase`` and skip outcomes/key-terms
    (their structure lives in the capstone-week template's deliverables
    + activities + rubric instead). Discrimination is done by the parent
    ``Unit.is_capstone`` flag — see ``Unit.validate_week_shape_matches_kind``.
    """

    model_config = ConfigDict(extra="forbid")

    n: int = Field(ge=1, description="Global week number, W1..W_total.")
    slug: str = Field(pattern=SLUG_PATTERN)
    title: str = Field(min_length=1)
    focus: str | None = Field(
        default=None,
        description="One-line central concept; required for standard weeks.",
    )
    outcome_phrases: list[str] | None = Field(
        default=None,
        description="Standard weeks: 3 or 4 entries. Each must start with an "
        "allowed action verb.",
    )
    key_term_names: list[str] | None = Field(
        default=None,
        description="Standard weeks: 6 to 8 entries. Term names only — "
        "definitions are filled by the bulk-fill phase.",
    )
    phase: CapstonePhase | None = Field(
        default=None,
        description="Capstone weeks only: which phase of the capstone arc.",
    )


# ---------------------------------------------------------------------------
# Unit
# ---------------------------------------------------------------------------


class Unit(BaseModel):
    """One unit inside a course. Standard units carry the structural anchors
    (outcomes, key concepts, glossary terms) the bulk-fill phase needs to keep
    cross-file coherence; capstone units have a lighter shape because the
    capstone-week template owns most of their structure.
    """

    model_config = ConfigDict(extra="forbid")

    n: int = Field(ge=1, description="Per-course unit number (1, 2, ...).")
    slug: str = Field(pattern=SLUG_PATTERN)
    title: str = Field(min_length=1)
    focus: str | None = Field(default=None)
    weeks_start: int = Field(ge=1, description="First global week of this unit.")
    weeks_end: int = Field(ge=1, description="Last global week (inclusive).")
    is_capstone: bool = False
    outcome_phrases: list[str] | None = Field(
        default=None,
        description="Standard units: exactly 6 entries, each verb-gated.",
    )
    key_concepts: list[str] | None = Field(
        default=None,
        description="Standard units: exactly 8 entries.",
    )
    glossary_terms: list[str] | None = Field(
        default=None,
        description="Standard units: exactly 12 entries. Term names only.",
    )
    weeks: list[Week] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_week_range(self) -> Unit:
        """``weeks_end`` must be >= ``weeks_start``."""
        if self.weeks_end < self.weeks_start:
            raise ValueError(
                f"unit {self.slug}: weeks_end ({self.weeks_end}) < "
                f"weeks_start ({self.weeks_start})"
            )
        return self

    @model_validator(mode="after")
    def validate_week_slugs_unique(self) -> Unit:
        """No two weeks in the same unit may share a slug."""
        slugs = [w.slug for w in self.weeks]
        dupes = [s for s, c in Counter(slugs).items() if c > 1]
        if dupes:
            raise ValueError(
                f"unit {self.slug}: duplicate week slugs {dupes!r}"
            )
        return self

    @model_validator(mode="after")
    def validate_shape_matches_kind(self) -> Unit:
        """Standard units have anchor counts + standard-week children;
        capstone units have phase-bearing children. Mismatches are hard fails
        because the bulk-fill dispatch (standard-week vs. capstone-week
        template) depends on this flag being honest.
        """
        where = f"unit {self.slug}"
        if self.is_capstone:
            for w in self.weeks:
                if w.phase is None:
                    raise ValueError(
                        f"{where}: capstone week {w.slug!r} must set "
                        f"`phase` (one of Proposal/Execution/Submission)"
                    )
                if w.outcome_phrases is not None or w.key_term_names is not None:
                    raise ValueError(
                        f"{where}: capstone week {w.slug!r} must not set "
                        "`outcome_phrases` or `key_term_names` — those are "
                        "for standard weeks"
                    )
            return self

        # Standard unit
        if self.outcome_phrases is None or len(self.outcome_phrases) != 6:
            raise ValueError(
                f"{where}: standard units need exactly 6 outcome_phrases, "
                f"got {len(self.outcome_phrases) if self.outcome_phrases else 0}"
            )
        _check_verbs(self.outcome_phrases, where)
        if self.key_concepts is None or len(self.key_concepts) != 8:
            raise ValueError(
                f"{where}: standard units need exactly 8 key_concepts, "
                f"got {len(self.key_concepts) if self.key_concepts else 0}"
            )
        if self.glossary_terms is None or len(self.glossary_terms) != 12:
            raise ValueError(
                f"{where}: standard units need exactly 12 glossary_terms, "
                f"got {len(self.glossary_terms) if self.glossary_terms else 0}"
            )
        # No duplicate glossary terms within the unit itself — the
        # course-level check catches inter-unit dupes; this catches typos
        # earlier.
        lowered = [t.strip().lower() for t in self.glossary_terms]
        dupes = [t for t, c in Counter(lowered).items() if c > 1]
        if dupes:
            raise ValueError(
                f"{where}: duplicate glossary_terms within unit: {dupes!r}"
            )

        # Standard weeks must have outcomes (3 or 4) and key terms (6 to 8),
        # and must not carry a capstone phase.
        for w in self.weeks:
            wwhere = f"{where}/week {w.slug}"
            if w.phase is not None:
                raise ValueError(
                    f"{wwhere}: standard weeks must not set `phase`"
                )
            if w.outcome_phrases is None or not (3 <= len(w.outcome_phrases) <= 4):
                raise ValueError(
                    f"{wwhere}: standard weeks need 3 or 4 outcome_phrases, "
                    f"got {len(w.outcome_phrases) if w.outcome_phrases else 0}"
                )
            _check_verbs(w.outcome_phrases, wwhere)
            if w.key_term_names is None or not (6 <= len(w.key_term_names) <= 8):
                raise ValueError(
                    f"{wwhere}: standard weeks need 6 to 8 key_term_names, "
                    f"got {len(w.key_term_names) if w.key_term_names else 0}"
                )
        return self


# ---------------------------------------------------------------------------
# Course
# ---------------------------------------------------------------------------


class Course(BaseModel):
    """One course in the degree. The final course in the degree must be a
    capstone (see ``Outline.validate_last_course_is_capstone``)."""

    model_config = ConfigDict(extra="forbid")

    n: int = Field(ge=1)
    slug: str = Field(pattern=SLUG_PATTERN)
    title: str = Field(min_length=1)
    focus: str = Field(min_length=1)
    tier: Tier
    weeks_start: int = Field(ge=1)
    weeks_end: int = Field(ge=1)
    key_capability: str | None = None
    is_capstone: bool = False
    themes_emphasized: list[int] | None = Field(
        default=None,
        description="Indices into Degree.themes (0..3) that this course "
        "exercises most. Not load-bearing for the schema — informational.",
    )
    units: list[Unit] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_week_range(self) -> Course:
        if self.weeks_end < self.weeks_start:
            raise ValueError(
                f"course {self.slug}: weeks_end ({self.weeks_end}) < "
                f"weeks_start ({self.weeks_start})"
            )
        return self

    @model_validator(mode="after")
    def validate_unit_slugs_unique(self) -> Course:
        slugs = [u.slug for u in self.units]
        dupes = [s for s, c in Counter(slugs).items() if c > 1]
        if dupes:
            raise ValueError(
                f"course {self.slug}: duplicate unit slugs {dupes!r}"
            )
        return self

    @model_validator(mode="after")
    def validate_no_duplicate_glossary_in_course(self) -> Course:
        """No two units in the same course may define the same glossary term
        — bulk-fill cannot reconcile two definitions and the resulting unit
        files would silently contradict each other."""
        all_terms: list[tuple[str, str]] = []  # (lowered_term, unit_slug)
        for u in self.units:
            if u.glossary_terms:
                for t in u.glossary_terms:
                    all_terms.append((t.strip().lower(), u.slug))
        seen: dict[str, str] = {}
        dupes: list[tuple[str, str, str]] = []  # (term, first_unit, second_unit)
        for term, unit in all_terms:
            if term in seen and seen[term] != unit:
                dupes.append((term, seen[term], unit))
            else:
                seen.setdefault(term, unit)
        if dupes:
            raise ValueError(
                f"course {self.slug}: duplicate glossary terms across units: "
                f"{dupes!r}"
            )
        return self

    @model_validator(mode="after")
    def validate_capstone_propagation(self) -> Course:
        """A capstone course must have at least one capstone unit; a
        non-capstone course must not have any. Bulk-fill dispatch reads
        ``Unit.is_capstone`` to pick the capstone-week template, so this
        flag must be consistent end-to-end."""
        capstone_units = [u for u in self.units if u.is_capstone]
        if self.is_capstone and not capstone_units:
            raise ValueError(
                f"course {self.slug}: is_capstone=True but no unit has "
                "is_capstone=True"
            )
        if not self.is_capstone and capstone_units:
            raise ValueError(
                f"course {self.slug}: not a capstone but units "
                f"{[u.slug for u in capstone_units]} are marked capstone"
            )
        return self


# ---------------------------------------------------------------------------
# Degree (root)
# ---------------------------------------------------------------------------


class DegreeMetadata(BaseModel):
    """Top-level degree information. The lists here are degree-level anchors
    that the bulk-fill phase uses for cross-course coherence (themes recur in
    multiple courses; program outcomes span multiple courses)."""

    model_config = ConfigDict(extra="forbid")

    slug: str = Field(pattern=SLUG_PATTERN)
    title: str = Field(min_length=1)
    tier_reached: Tier
    prerequisites: str = Field(min_length=1)
    total_courses: int = Field(
        ge=4,
        le=6,
        description="Must be 4, 5, or 6 — the only valid degree sizes "
        "per DEGREE_TEMPLATE.md.",
    )
    themes: list[str] = Field(
        min_length=4,
        max_length=4,
        description="Exactly 4 recurring tensions across all courses.",
    )
    program_outcome_phrases: list[str] = Field(
        min_length=6,
        max_length=6,
        description="Exactly 6 outcomes, each starting with an allowed "
        "action verb.",
    )

    @model_validator(mode="after")
    def validate_program_outcome_verbs(self) -> DegreeMetadata:
        _check_verbs(self.program_outcome_phrases, "degree program outcomes")
        return self


class Outline(BaseModel):
    """The root outline. Equivalent to the on-disk ``degree_outline.json``."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = Field(
        description="Schema version. Bumped when the JSON shape changes "
        "incompatibly."
    )
    degree: DegreeMetadata
    courses: list[Course] = Field(min_length=4, max_length=6)

    @model_validator(mode="after")
    def validate_course_count_matches_metadata(self) -> Outline:
        if len(self.courses) != self.degree.total_courses:
            raise ValueError(
                f"len(courses)={len(self.courses)} != "
                f"degree.total_courses={self.degree.total_courses}"
            )
        return self

    @model_validator(mode="after")
    def validate_course_slugs_unique(self) -> Outline:
        slugs = [c.slug for c in self.courses]
        dupes = [s for s, c in Counter(slugs).items() if c > 1]
        if dupes:
            raise ValueError(f"duplicate course slugs {dupes!r}")
        return self

    @model_validator(mode="after")
    def validate_last_course_is_capstone(self) -> Outline:
        """The final course is always the capstone. Earlier courses must not
        be marked capstone — bulk-fill picks the capstone-week template only
        for the final course's final unit."""
        if not self.courses[-1].is_capstone:
            raise ValueError(
                f"the last course ({self.courses[-1].slug!r}) must have "
                "is_capstone=True"
            )
        bad = [c.slug for c in self.courses[:-1] if c.is_capstone]
        if bad:
            raise ValueError(
                f"only the final course may be capstone; got non-final "
                f"capstone courses {bad!r}"
            )
        return self

    @model_validator(mode="after")
    def validate_dense_week_numbering(self) -> Outline:
        """Weeks are global (W1..W_total) and dense: course 1 starts at W1,
        course N starts where course N-1 ended + 1, each unit tiles its
        course, each week tiles its unit. Sparse or overlapping numbering
        means the file tree would have orphan or colliding week files."""
        expected_w = 1
        for c in self.courses:
            if c.weeks_start != expected_w:
                raise ValueError(
                    f"course {c.slug}: weeks_start={c.weeks_start}, "
                    f"expected {expected_w} (must follow previous course)"
                )
            inner_w = c.weeks_start
            for u in c.units:
                if u.weeks_start != inner_w:
                    raise ValueError(
                        f"unit {c.slug}/{u.slug}: weeks_start={u.weeks_start}, "
                        f"expected {inner_w}"
                    )
                for w in u.weeks:
                    if w.n != inner_w:
                        raise ValueError(
                            f"week {c.slug}/{u.slug}/{w.slug}: n={w.n}, "
                            f"expected global W{inner_w}"
                        )
                    inner_w += 1
                if u.weeks_end != inner_w - 1:
                    raise ValueError(
                        f"unit {c.slug}/{u.slug}: weeks_end={u.weeks_end}, "
                        f"expected {inner_w - 1} (sum of its weeks)"
                    )
            if c.weeks_end != inner_w - 1:
                raise ValueError(
                    f"course {c.slug}: weeks_end={c.weeks_end}, "
                    f"expected {inner_w - 1} (sum of its units)"
                )
            expected_w = inner_w
        return self


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def validate_outline(data: dict | str | Path) -> Outline:
    """Parse and validate an outline.

    Args:
        data: An already-parsed dict, a JSON string, or a ``Path`` to a
            JSON file on disk.

    Returns:
        The validated :class:`Outline`.

    Raises:
        pydantic.ValidationError: when any schema rule fails. The error
            aggregates every violation; do not retry blindly.
        FileNotFoundError: when ``data`` is a Path that does not exist.
        json.JSONDecodeError: when ``data`` is a string or file with
            invalid JSON.
    """
    if isinstance(data, Path):
        data = json.loads(data.read_text(encoding="utf-8"))
    elif isinstance(data, str):
        data = json.loads(data)
    return Outline.model_validate(data)


__all__ = [
    "ACTION_VERBS",
    "CapstonePhase",
    "Course",
    "DegreeMetadata",
    "Outline",
    "SLUG_PATTERN",
    "Tier",
    "Unit",
    "Week",
    "validate_outline",
]
