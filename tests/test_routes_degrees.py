"""Tests for Phase 24: the /degrees routes.

Focus is the HTTP + template contract (page vs fragment, checkpoint markers,
SSE wiring, error fragments). The pipeline itself is covered by
``test_degree_factory.py``, so here the factory functions are mocked — these
tests don't call a real (or mocked-transport) Ollama through the build.

Each test uses the shared ``make_client`` fixture from ``tests/test_routes.py``
inside a ``with`` block so the app lifespan opens ``app.state.db``.
"""

import json

import httpx

from app import degree_factory
from app.degree_factory import DegreeDraft, DegreeForm, DegreeJob

from tests.test_routes import (  # noqa: F401 — fixture re-exports
    ClientFactory,
    make_client,
)


def _noop_handler(request: httpx.Request) -> httpx.Response:
    """Trivial /api/chat stub — the factory is mocked, so it's never hit."""
    return httpx.Response(200, json={"message": {"content": "{}"}})


def _sample_draft(draft_id: str = "d1", course_count: int = 4) -> DegreeDraft:
    form = DegreeForm(
        subject="s", learner="l", tier_benchmark="b", capstone="c",
        course_count=course_count,
    )
    degree_meta = {
        "slug": "physics", "title": "Physics", "tier_reached": "advanced",
        "prerequisites": "calculus", "themes": [f"theme {i}" for i in range(4)],
        "program_outcome_phrases": [f"Derive result {i}" for i in range(6)],
    }
    courses = [
        {"slug": f"c{i}", "title": f"Course {i}", "focus": "focus",
         "tier": "intro", "key_capability": "k", "week_count": 6}
        for i in range(1, course_count + 1)
    ]
    degree_factory.tile_courses(courses)
    return DegreeDraft(draft_id=draft_id, form=form,
                       degree_meta=degree_meta, courses=courses)


# ---------------------------------------------------------------------------
# GET /degrees
# ---------------------------------------------------------------------------


def test_degrees_full_render_has_shell_and_form(make_client, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    with make_client(_noop_handler) as client:
        r = client.get("/degrees")
    assert r.status_code == 200
    assert "<html" in r.text  # full shell, not just a fragment
    assert 'class="sidebar"' in r.text
    assert 'hx-post="/degrees/draft"' in r.text
    assert 'name="subject"' in r.text


def test_degrees_fragment_on_hx_request(make_client, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    with make_client(_noop_handler) as client:
        r = client.get("/degrees", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "<html" not in r.text  # fragment only
    assert 'hx-post="/degrees/draft"' in r.text


def test_degrees_unconfigured_workspace_hides_form(make_client) -> None:
    # FILE_TOOL_ROOT is unset by make_client -> workspace not configured.
    with make_client(_noop_handler) as client:
        r = client.get("/degrees", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "not configured" in r.text
    assert 'hx-post="/degrees/draft"' not in r.text


def test_degrees_lists_saved_outlines(make_client, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    with make_client(_noop_handler) as client:
        # Write AFTER lifespan startup so the one-time legacy-workspace
        # migration doesn't relocate the dir into default/.
        (tmp_path / "physics").mkdir()
        (tmp_path / "physics" / "degree_outline.json").write_text(
            json.dumps({"degree": {"title": "My Physics Degree"}})
        )
        r = client.get("/degrees", headers={"HX-Request": "true"})
    assert "My Physics Degree" in r.text
    assert "/degrees/physics/outline.json" in r.text


# ---------------------------------------------------------------------------
# POST /degrees/draft
# ---------------------------------------------------------------------------


_FORM = {
    "subject": "mechanics", "learner": "hobbyist", "tier_benchmark": "Goldstein",
    "capstone": "thesis", "course_count": "4",
}


def test_draft_returns_checkpoint(make_client, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))

    async def _fake_create_draft(client, form, **kwargs):
        draft = _sample_draft(course_count=form.course_count)
        degree_factory.degree_drafts[draft.draft_id] = draft
        return draft

    monkeypatch.setattr(degree_factory, "create_draft", _fake_create_draft)
    with make_client(_noop_handler) as client:
        r = client.post("/degrees/draft", data=_FORM)
    assert r.status_code == 200
    assert 'data-draft-id="d1"' in r.text
    assert "/degrees/draft/d1/build" in r.text
    assert "/degrees/draft/d1/regenerate" in r.text
    assert "Course 1" in r.text
    assert "capstone" in r.text  # the last course's badge


def test_draft_unconfigured_workspace_inline_error(make_client) -> None:
    with make_client(_noop_handler) as client:
        r = client.post("/degrees/draft", data=_FORM)
    assert r.status_code == 200
    assert "form-error" in r.text
    assert "not configured" in r.text


def test_draft_ollama_unavailable_inline_error(make_client, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))

    async def _boom(client, form, **kwargs):
        raise degree_factory.OllamaUnavailable("ollama is down")

    monkeypatch.setattr(degree_factory, "create_draft", _boom)
    with make_client(_noop_handler) as client:
        r = client.post("/degrees/draft", data=_FORM)
    assert r.status_code == 200
    assert "form-error" in r.text
    assert "unavailable" in r.text.lower()


def test_draft_factory_error_inline_error(make_client, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))

    async def _boom(client, form, **kwargs):
        raise degree_factory.DegreeFactoryError("model wandered off")

    monkeypatch.setattr(degree_factory, "create_draft", _boom)
    with make_client(_noop_handler) as client:
        r = client.post("/degrees/draft", data=_FORM)
    assert r.status_code == 200
    assert "form-error" in r.text


# ---------------------------------------------------------------------------
# POST /degrees/draft/{id}/regenerate
# ---------------------------------------------------------------------------


def test_regenerate_updates_existing_draft(make_client, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    draft = _sample_draft(draft_id="dR")
    degree_factory.degree_drafts["dR"] = draft

    async def _fake_regen(client, d, **kwargs):
        return d

    monkeypatch.setattr(degree_factory, "regenerate_draft", _fake_regen)
    with make_client(_noop_handler) as client:
        r = client.post("/degrees/draft/dR/regenerate", data={"note": "deeper"})
    assert r.status_code == 200
    assert 'data-draft-id="dR"' in r.text


def test_regenerate_unknown_draft_404(make_client) -> None:
    with make_client(_noop_handler) as client:
        r = client.post("/degrees/draft/nope/regenerate", data={"note": ""})
    assert r.status_code == 404


def test_regenerate_ollama_unavailable_inline_error(make_client, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    degree_factory.degree_drafts["dRX"] = _sample_draft(draft_id="dRX")

    async def _boom(client, d, **kwargs):
        raise degree_factory.OllamaUnavailable("ollama down")

    monkeypatch.setattr(degree_factory, "regenerate_draft", _boom)
    with make_client(_noop_handler) as client:
        r = client.post("/degrees/draft/dRX/regenerate", data={"note": ""})
    assert r.status_code == 200
    assert "form-error" in r.text
    assert "unavailable" in r.text.lower()


def test_regenerate_factory_error_inline_error(make_client, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    degree_factory.degree_drafts["dRY"] = _sample_draft(draft_id="dRY")

    async def _boom(client, d, **kwargs):
        raise degree_factory.DegreeFactoryError("model wandered off")

    monkeypatch.setattr(degree_factory, "regenerate_draft", _boom)
    with make_client(_noop_handler) as client:
        r = client.post("/degrees/draft/dRY/regenerate", data={"note": ""})
    assert r.status_code == 200
    assert "form-error" in r.text


# ---------------------------------------------------------------------------
# POST /degrees/draft/{id}/build
# ---------------------------------------------------------------------------


def _fake_course_starter(job_id: str):
    """A start_course_build stand-in that plants an empty job under job_id."""
    async def _start(*, client, draft, **kwargs):
        job = DegreeJob(job_id=job_id, degree_slug=draft.degree_meta["slug"])
        degree_factory.degree_jobs[job_id] = job
        return job
    return _start


def test_build_starts_first_course_and_returns_sse(make_client, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    draft = _sample_draft(draft_id="dB")
    draft.built_courses = [{"stale": True}]  # prior partial build to be reset
    degree_factory.degree_drafts["dB"] = draft
    monkeypatch.setattr(degree_factory, "start_course_build", _fake_course_starter("jB"))
    with make_client(_noop_handler) as client:
        r = client.post("/degrees/draft/dB/build")
    assert r.status_code == 200
    assert 'sse-connect="/degrees/jobs/jB/events"' in r.text
    assert draft.built_courses == []  # reset for a fresh build


def test_build_unknown_draft_404(make_client) -> None:
    with make_client(_noop_handler) as client:
        r = client.post("/degrees/draft/nope/build")
    assert r.status_code == 404


def test_regenerate_course_spawns_course_job(make_client, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    degree_factory.degree_drafts["dRC"] = _sample_draft(draft_id="dRC")
    monkeypatch.setattr(degree_factory, "start_course_build", _fake_course_starter("jRC"))
    with make_client(_noop_handler) as client:
        r = client.post("/degrees/draft/dRC/regenerate-course")
    assert r.status_code == 200
    assert 'sse-connect="/degrees/jobs/jRC/events"' in r.text


def test_approve_course_appends_and_builds_next(make_client, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    draft = _sample_draft(draft_id="dA")  # 4 courses
    degree_factory.degree_drafts["dA"] = draft
    job0 = DegreeJob(job_id="j0", degree_slug=draft.degree_meta["slug"])
    job0.built_course = {"slug": "c1", "title": "Course 1", "units": []}
    job0.done = True
    degree_factory.degree_jobs["j0"] = job0
    monkeypatch.setattr(degree_factory, "start_course_build", _fake_course_starter("jNEXT"))
    with make_client(_noop_handler) as client:
        r = client.post("/degrees/draft/dA/approve/j0")
    assert r.status_code == 200
    assert 'sse-connect="/degrees/jobs/jNEXT/events"' in r.text
    assert len(draft.built_courses) == 1  # course 0 approved
    assert job0.approved is True


def test_approve_last_course_assembles_and_finishes(make_client, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    draft = _sample_draft(draft_id="dF", course_count=4)
    draft.built_courses = [{}, {}, {}]  # first three already approved
    degree_factory.degree_drafts["dF"] = draft
    job3 = DegreeJob(job_id="j3", degree_slug=draft.degree_meta["slug"])
    job3.built_course = {}
    job3.done = True
    degree_factory.degree_jobs["j3"] = job3
    # Skip real validation/write — that's covered in the factory tests.
    monkeypatch.setattr(
        degree_factory, "assemble_and_write",
        lambda d: tmp_path / "physics" / "degree_outline.json",
    )
    with make_client(_noop_handler) as client:
        r = client.post("/degrees/draft/dF/approve/j3")
    assert r.status_code == 200
    assert "Built" in r.text  # the result fragment
    assert len(draft.built_courses) == 4


def test_approve_finish_validation_error_inline(make_client, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    draft = _sample_draft(draft_id="dFE", course_count=4)
    draft.built_courses = [{}, {}, {}]
    degree_factory.degree_drafts["dFE"] = draft
    job3 = DegreeJob(job_id="j3e", degree_slug=draft.degree_meta["slug"])
    job3.built_course = {}
    degree_factory.degree_jobs["j3e"] = job3

    def _boom(d):
        raise degree_factory.DegreeFactoryError("could not assemble")

    monkeypatch.setattr(degree_factory, "assemble_and_write", _boom)
    with make_client(_noop_handler) as client:
        r = client.post("/degrees/draft/dFE/approve/j3e")
    assert r.status_code == 200
    assert "form-error" in r.text


def test_approve_unknown_job_404(make_client, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    degree_factory.degree_drafts["dU"] = _sample_draft(draft_id="dU")
    with make_client(_noop_handler) as client:
        r = client.post("/degrees/draft/dU/approve/ghostjob")
    assert r.status_code == 404


def test_build_rest_approves_then_builds_remaining(make_client, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    draft = _sample_draft(draft_id="dBR")
    degree_factory.degree_drafts["dBR"] = draft
    job0 = DegreeJob(job_id="jr0", degree_slug=draft.degree_meta["slug"])
    job0.built_course = {"slug": "c1"}
    job0.done = True
    degree_factory.degree_jobs["jr0"] = job0

    async def _fake_rest(*, client, draft, **kwargs):
        nj = DegreeJob(job_id="jREST", degree_slug=draft.degree_meta["slug"])
        degree_factory.degree_jobs["jREST"] = nj
        return nj

    monkeypatch.setattr(degree_factory, "start_build", _fake_rest)
    with make_client(_noop_handler) as client:
        r = client.post("/degrees/draft/dBR/build-rest/jr0")
    assert r.status_code == 200
    assert 'sse-connect="/degrees/jobs/jREST/events"' in r.text
    assert len(draft.built_courses) == 1  # the reviewed course was approved first


# ---------------------------------------------------------------------------
# GET /degrees/jobs/{id}/events
# ---------------------------------------------------------------------------


def test_events_stream_replays_progress_and_done(make_client) -> None:
    job = DegreeJob(job_id="jE", degree_slug="physics")
    job.events.append(("progress", "<div>building course 1</div>"))
    job.events.append(("done", "<div>finished</div>"))
    job.done = True
    degree_factory.degree_jobs["jE"] = job
    with make_client(_noop_handler) as client:
        r = client.get("/degrees/jobs/jE/events")
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    assert "event: progress" in r.text
    assert "event: done" in r.text


def test_events_stream_disk_fallback_for_missing_job(make_client) -> None:
    with make_client(_noop_handler) as client:
        r = client.get("/degrees/jobs/ghost/events")
    assert r.status_code == 200
    assert "event: done" in r.text
    assert "degree-build-ghost" in r.text


# ---------------------------------------------------------------------------
# GET /degrees/{slug}/outline.json
# ---------------------------------------------------------------------------


def test_outline_json_returns_saved_file(make_client, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    with make_client(_noop_handler) as client:
        (tmp_path / "physics").mkdir()
        (tmp_path / "physics" / "degree_outline.json").write_text(
            json.dumps({"version": 1, "degree": {"title": "Physics"}})
        )
        r = client.get("/degrees/physics/outline.json")
    assert r.status_code == 200
    assert r.json()["degree"]["title"] == "Physics"


def _write_partial(tmp_path, slug="physics", title="Physics WIP", built=2, total=4):
    d = tmp_path / slug
    d.mkdir(exist_ok=True)
    (d / "degree_outline.partial.json").write_text(json.dumps({
        "version": 1,
        "form": {"subject": "s", "learner": "l", "tier_benchmark": "b",
                 "capstone": "c", "course_count": total},
        "degree_meta": {"slug": slug, "title": title, "tier_reached": "intro",
                        "prerequisites": "none", "themes": ["a", "b", "c", "d"],
                        "program_outcome_phrases": ["Derive x"] * 6},
        "courses": [{"slug": f"c{i}", "title": f"C{i}", "week_count": 6} for i in range(total)],
        "built_courses": [{"slug": f"b{i}", "title": f"Built {i}", "units": []}
                          for i in range(built)],
    }))


def test_degrees_lists_in_progress(make_client, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    with make_client(_noop_handler) as client:
        _write_partial(tmp_path, built=2, total=4)
        r = client.get("/degrees", headers={"HX-Request": "true"})
    assert "In progress" in r.text
    assert "Physics WIP" in r.text
    assert "2 / 4 courses built" in r.text
    assert 'hx-post="/degrees/resume/physics"' in r.text


def test_degrees_excludes_finished_from_in_progress(make_client, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    with make_client(_noop_handler) as client:
        _write_partial(tmp_path)  # partial...
        (tmp_path / "physics" / "degree_outline.json").write_text('{"degree": {"title": "Done"}}')
        r = client.get("/degrees", headers={"HX-Request": "true"})
    # the finished degree shows under Saved outlines, not In progress
    assert 'hx-post="/degrees/resume/physics"' not in r.text


def test_resume_loads_partial_and_shows_panel(make_client, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    with make_client(_noop_handler) as client:
        _write_partial(tmp_path, built=1, total=4)
        r = client.post("/degrees/resume/physics")
    assert r.status_code == 200
    assert "Resume: Physics WIP" in r.text
    assert "1 of 4 courses already built" in r.text
    assert "Built 0" in r.text  # the approved course title
    assert "/regenerate-course" in r.text  # Build next course
    assert "/build-rest" in r.text          # Build all remaining
    assert "data-draft-id=" in r.text


def test_degrees_in_progress_tolerates_bad_partial(make_client, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    with make_client(_noop_handler) as client:
        d = tmp_path / "wip"
        d.mkdir()
        (d / "degree_outline.partial.json").write_text("{ not json")
        r = client.get("/degrees", headers={"HX-Request": "true"})
    assert r.status_code == 200
    # listed by directory name, no crash, resume still offered
    assert 'hx-post="/degrees/resume/wip"' in r.text


def test_resume_missing_404(make_client, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    with make_client(_noop_handler) as client:
        r = client.post("/degrees/resume/ghost")
    assert r.status_code == 404


def test_build_rest_resume_no_job_spawns_full_build(make_client, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    degree_factory.degree_drafts["dRR"] = _sample_draft(draft_id="dRR")

    async def _fake_rest(*, client, draft, **kwargs):
        nj = DegreeJob(job_id="jRR", degree_slug=draft.degree_meta["slug"])
        degree_factory.degree_jobs["jRR"] = nj
        return nj

    monkeypatch.setattr(degree_factory, "start_build", _fake_rest)
    with make_client(_noop_handler) as client:
        r = client.post("/degrees/draft/dRR/build-rest")
    assert r.status_code == 200
    assert 'sse-connect="/degrees/jobs/jRR/events"' in r.text


def test_approve_persists_partial(make_client, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    draft = _sample_draft(draft_id="dAP")  # slug "physics", 4 courses
    degree_factory.degree_drafts["dAP"] = draft
    job0 = DegreeJob(job_id="jap0", degree_slug=draft.degree_meta["slug"])
    job0.built_course = {"slug": "c1", "title": "Course 1", "units": []}
    job0.done = True
    degree_factory.degree_jobs["jap0"] = job0
    monkeypatch.setattr(degree_factory, "start_course_build", _fake_course_starter("japN"))
    with make_client(_noop_handler) as client:
        r = client.post("/degrees/draft/dAP/approve/jap0")
    assert r.status_code == 200
    # approval wrote the resume file with the one approved course
    assert (tmp_path / "physics" / "degree_outline.partial.json").is_file()


def test_outline_json_missing_404(make_client, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    with make_client(_noop_handler) as client:
        r = client.get("/degrees/ghost/outline.json")
    assert r.status_code == 404


def test_outline_json_unconfigured_workspace_404(make_client) -> None:
    # FILE_TOOL_ROOT unset by make_client.
    with make_client(_noop_handler) as client:
        r = client.get("/degrees/physics/outline.json")
    assert r.status_code == 404


def test_degrees_listing_tolerates_stray_file_and_bad_json(make_client, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    with make_client(_noop_handler) as client:
        (tmp_path / "loose.txt").write_text("not a degree dir")  # non-dir at root
        (tmp_path / "broken").mkdir()
        (tmp_path / "broken" / "degree_outline.json").write_text("{ not json")
        r = client.get("/degrees", headers={"HX-Request": "true"})
    assert r.status_code == 200
    # the unparseable outline is still listed, title falls back to the dir name
    assert "broken" in r.text
