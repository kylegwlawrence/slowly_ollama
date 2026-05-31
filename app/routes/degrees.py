"""Phase 24: the ``/degrees`` section — form-driven degree-outline factory.

A top-level page (modeled on :mod:`app.routes.settings`): a small brief form
generates a degree skeleton + course list (the checkpoint); the user approves or
regenerates; approval spawns a background build that streams progress over SSE
and writes ``<slug>/degree_outline.json`` to the workspace.

Routes:
    GET  /degrees                              — page or fragment
    POST /degrees/draft                        — run Stage A, return checkpoint
    POST /degrees/draft/{id}/regenerate        — re-run Stage A in place
    POST /degrees/draft/{id}/build             — spawn the build, return progress
    GET  /degrees/jobs/{id}/events             — SSE progress stream
    GET  /degrees/{slug}/outline.json          — read a saved outline
"""

from __future__ import annotations

import html
import json
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import ValidationError

from app import degree_factory, queries
from app.config import file_tool_root
from app.degree_factory import DegreeForm
from app.dependencies import DB, OllamaClient
from app.ollama import OllamaProtocolError, OllamaUnavailable
from app.templates import templates

router = APIRouter()


def _list_existing_outlines() -> list[dict]:
    """Return ``[{slug, title}]`` for every ``<slug>/degree_outline.json`` under
    the workspace root, alphabetical. Best-effort — unreadable/invalid files are
    skipped (title falls back to the directory name)."""
    root = file_tool_root()
    if root is None or not root.exists():
        return []
    outlines: list[dict] = []
    for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir():
            continue
        outline_file = child / "degree_outline.json"
        if not outline_file.is_file():
            continue
        title = child.name
        try:
            data = json.loads(outline_file.read_text(encoding="utf-8"))
            title = (data.get("degree") or {}).get("title") or child.name
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
        outlines.append({"slug": child.name, "title": title})
    return outlines


def _list_in_progress() -> list[dict]:
    """Return ``[{slug, title, built, total}]`` for degrees with a saved partial
    but no finished outline — interrupted per-course builds the user can resume."""
    root = file_tool_root()
    if root is None or not root.exists():
        return []
    items: list[dict] = []
    for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir():
            continue
        partial = child / degree_factory.PARTIAL_NAME
        if not partial.is_file() or (child / degree_factory.OUTLINE_NAME).is_file():
            continue  # only in-progress: partial present, final not yet written
        title, built, total = child.name, 0, 0
        try:
            data = json.loads(partial.read_text(encoding="utf-8"))
            title = (data.get("degree_meta") or {}).get("title") or child.name
            built = len(data.get("built_courses") or [])
            total = len(data.get("courses") or [])
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
        items.append({"slug": child.name, "title": title, "built": built, "total": total})
    return items


def _inline_error(message: str) -> HTMLResponse:
    """A 200 error fragment that swaps into ``#degree-checkpoint`` so the user
    sees what went wrong in place (HTMX skips swaps on non-2xx)."""
    return HTMLResponse(
        f'<p class="form-error" role="alert">{html.escape(message)}</p>'
    )


def _clamp_course_count(n: int) -> int:
    """Force the course count into the schema's valid {4, 5, 6} band."""
    return max(4, min(6, n))


def _factory_model(db) -> str:
    """The model the factory runs on: the app's configured default when set,
    else the factory's installed fallback. Keeps the degree build on a model
    the user actually has, with no separate pull."""
    return queries.get_default_model(db) or degree_factory.DEFAULT_MODEL


def _get_draft_or_404(draft_id: str) -> degree_factory.DegreeDraft:
    draft = degree_factory.degree_drafts.get(draft_id)
    if draft is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Draft expired — please start again."
        )
    return draft


def _building(request: Request, job: degree_factory.DegreeJob) -> Response:
    """Render the SSE-wired build-progress fragment for a job."""
    return templates.TemplateResponse(
        request=request, name="_degree_building.html", context={"job": job}
    )


@router.get("/degrees", response_class=HTMLResponse)
def degrees_endpoint(request: Request, db: DB) -> Response:
    """The Degrees page (full shell on a direct hit, fragment for HTMX swaps)."""
    ctx = {
        "outlines": _list_existing_outlines(),
        "in_progress": _list_in_progress(),
        "workspace_configured": file_tool_root() is not None,
    }
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request=request, name="_degrees.html", context=ctx
        )
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "layout": "degrees",
            "project": None,
            "conversation": None,
            "active_chat_id": None,
            "projects": queries.list_projects(db),
            "active_project_id": None,
            **ctx,
        },
    )


@router.post("/degrees/draft", response_class=HTMLResponse)
async def degree_draft_endpoint(
    request: Request,
    client: OllamaClient,
    db: DB,
    subject: Annotated[str, Form()],
    learner: Annotated[str, Form()],
    tier_benchmark: Annotated[str, Form()],
    capstone: Annotated[str, Form()],
    course_count: Annotated[int, Form()],
) -> Response:
    """Run Stage A and return the checkpoint (degree summary + course list)."""
    if file_tool_root() is None:
        return _inline_error("Workspace not configured (FILE_TOOL_ROOT is unset).")
    form = DegreeForm(
        subject=subject.strip(),
        learner=learner.strip(),
        tier_benchmark=tier_benchmark.strip(),
        capstone=capstone.strip(),
        course_count=_clamp_course_count(course_count),
    )
    try:
        draft = await degree_factory.create_draft(
            client, form, model=_factory_model(db),
            num_ctx=queries.get_default_num_ctx(db),
        )
    except OllamaUnavailable as e:
        return _inline_error(f"Ollama is unavailable: {e}")
    except (OllamaProtocolError, degree_factory.DegreeFactoryError) as e:
        return _inline_error(f"Couldn't generate the course list: {e}")
    return templates.TemplateResponse(
        request=request, name="_degree_checkpoint.html", context={"draft": draft}
    )


@router.post("/degrees/draft/{draft_id}/regenerate", response_class=HTMLResponse)
async def degree_regenerate_endpoint(
    draft_id: str,
    request: Request,
    client: OllamaClient,
    db: DB,
    note: Annotated[str, Form()] = "",
) -> Response:
    """Re-run Stage A for an existing draft, optionally nudged by ``note``."""
    draft = degree_factory.degree_drafts.get(draft_id)
    if draft is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Draft expired — please start again."
        )
    try:
        draft = await degree_factory.regenerate_draft(
            client, draft, note=note, model=_factory_model(db),
            num_ctx=queries.get_default_num_ctx(db),
        )
    except OllamaUnavailable as e:
        return _inline_error(f"Ollama is unavailable: {e}")
    except (OllamaProtocolError, degree_factory.DegreeFactoryError) as e:
        return _inline_error(f"Couldn't regenerate the course list: {e}")
    return templates.TemplateResponse(
        request=request, name="_degree_checkpoint.html", context={"draft": draft}
    )


@router.post("/degrees/draft/{draft_id}/build", response_class=HTMLResponse)
async def degree_build_endpoint(
    draft_id: str, request: Request, client: OllamaClient, db: DB
) -> Response:
    """Start the per-course build loop: build the first course, then pause for
    review. Resets any prior partial build for this draft."""
    draft = _get_draft_or_404(draft_id)
    draft.built_courses = []
    job = await degree_factory.start_course_build(
        client=client, draft=draft, model=_factory_model(db),
        num_ctx=queries.get_default_num_ctx(db),
    )
    return _building(request, job)


@router.post(
    "/degrees/draft/{draft_id}/regenerate-course", response_class=HTMLResponse
)
async def degree_regenerate_course_endpoint(
    draft_id: str, request: Request, client: OllamaClient, db: DB
) -> Response:
    """Rebuild the course currently under review (the next un-approved one)."""
    draft = _get_draft_or_404(draft_id)
    job = await degree_factory.start_course_build(
        client=client, draft=draft, model=_factory_model(db),
        num_ctx=queries.get_default_num_ctx(db),
    )
    return _building(request, job)


@router.post(
    "/degrees/draft/{draft_id}/approve/{job_id}", response_class=HTMLResponse
)
async def degree_approve_course_endpoint(
    draft_id: str, job_id: str, request: Request, client: OllamaClient, db: DB
) -> Response:
    """Approve the reviewed course, then build the next one — or, when every
    course is approved, assemble + validate + write the outline."""
    draft = _get_draft_or_404(draft_id)
    job = degree_factory.degree_jobs.get(job_id)
    if job is None or job.built_course is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "That build is gone — please start again."
        )
    if not job.approved:
        draft.built_courses.append(job.built_course)
        job.approved = True
        degree_factory.persist_draft(draft)  # save progress after each approval

    if len(draft.built_courses) < len(draft.courses):
        next_job = await degree_factory.start_course_build(
            client=client, draft=draft, model=_factory_model(db),
            num_ctx=queries.get_default_num_ctx(db),
        )
        return _building(request, next_job)

    # All courses approved — the assemble step makes no model calls, so do it
    # synchronously and return the final result.
    try:
        path = degree_factory.assemble_and_write(draft)
    except (ValidationError, degree_factory.DegreeFactoryError) as e:
        return _inline_error(f"Couldn't finish the outline: {e}")
    return templates.TemplateResponse(
        request=request, name="_degree_result.html",
        context={"slug": draft.degree_meta["slug"], "path": str(path)},
    )


@router.post(
    "/degrees/draft/{draft_id}/build-rest/{job_id}", response_class=HTMLResponse
)
async def degree_build_rest_endpoint(
    draft_id: str, job_id: str, request: Request, client: OllamaClient, db: DB
) -> Response:
    """Approve the reviewed course, then build all remaining courses
    autonomously (the escape hatch) and write the outline."""
    draft = _get_draft_or_404(draft_id)
    job = degree_factory.degree_jobs.get(job_id)
    if job is not None and job.built_course is not None and not job.approved:
        draft.built_courses.append(job.built_course)
        job.approved = True
        degree_factory.persist_draft(draft)
    rest_job = await degree_factory.start_build(
        client=client, draft=draft, model=_factory_model(db),
        num_ctx=queries.get_default_num_ctx(db),
    )
    return _building(request, rest_job)


@router.post("/degrees/draft/{draft_id}/build-rest", response_class=HTMLResponse)
async def degree_build_rest_resume_endpoint(
    draft_id: str, request: Request, client: OllamaClient, db: DB
) -> Response:
    """Build all remaining courses of a resumed draft (no course pending
    approval — the Resume panel's 'Build all remaining' / 'Finish')."""
    draft = _get_draft_or_404(draft_id)
    rest_job = await degree_factory.start_build(
        client=client, draft=draft, model=_factory_model(db),
        num_ctx=queries.get_default_num_ctx(db),
    )
    return _building(request, rest_job)


@router.post("/degrees/resume/{slug}", response_class=HTMLResponse)
async def degree_resume_endpoint(slug: str, request: Request) -> Response:
    """Reload a saved partial into memory and show the resume panel."""
    draft = degree_factory.load_partial(slug)
    if draft is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "No saved progress for that degree."
        )
    return templates.TemplateResponse(
        request=request, name="_degree_resume.html",
        context={
            "draft": draft,
            "built": len(draft.built_courses),
            "total": len(draft.courses),
            "built_titles": [c.get("title", "?") for c in draft.built_courses],
        },
    )


@router.delete("/degrees/partials/{slug}", response_class=HTMLResponse)
def degree_delete_partial_endpoint(slug: str) -> Response:
    """Delete an in-progress partial (the Delete button on the In-progress list).

    The slug is re-sanitized to keep the unlink inside the workspace. Returns
    an empty 200 so the row's ``hx-swap="outerHTML"`` replaces it with nothing;
    404 when no partial exists (refuses a no-op so the UI can show feedback).
    """
    root = file_tool_root()
    if root is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not configured.")
    safe = degree_factory._slugify(slug, fallback="degree")
    partial = (root / safe / degree_factory.PARTIAL_NAME).resolve()
    if not partial.is_relative_to(root) or not partial.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No partial to delete.")
    degree_factory.delete_partial(safe)
    return HTMLResponse("")


@router.get("/degrees/jobs/{job_id}/events")
async def degree_events_endpoint(job_id: str) -> StreamingResponse:
    """SSE progress for a build. Attaches to the live job, or emits a single
    terminal event when the job is gone (only after a server restart — jobs
    aren't evicted on completion)."""
    job = degree_factory.degree_jobs.get(job_id)
    if job is not None:
        return StreamingResponse(
            degree_factory.consume_degree_job(job),
            media_type="text/event-stream",
        )
    return StreamingResponse(
        degree_factory.consume_degree_job_finished(job_id),
        media_type="text/event-stream",
    )


@router.get("/degrees/{slug}/outline.json")
def degree_outline_json_endpoint(slug: str) -> Response:
    """Return a saved ``degree_outline.json`` verbatim (the Saved-outlines links
    point here). The slug is re-sanitized so it can't escape the workspace."""
    root = file_tool_root()
    if root is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not configured.")
    safe = degree_factory._slugify(slug, fallback="degree")
    path = (root / safe / "degree_outline.json").resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Outline not found.")
    return Response(
        path.read_text(encoding="utf-8"), media_type="application/json"
    )
