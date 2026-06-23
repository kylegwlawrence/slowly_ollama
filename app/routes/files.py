"""Files-tab routes: list, view, download files in a project workspace.

Routes:
    GET /projects/{id}/files                 — directory listing
    GET /projects/{id}/files/view?path=...   — render one file
    GET /projects/{id}/files/download?path=… — stream as attachment

The view-shaping logic (``browse_workspace`` / ``read_workspace_file``)
lives in :mod:`app.projects`; these routes are thin wrappers around it.
"""

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, HTMLResponse

from app import queries
from app.dependencies import DB
from app.projects import (
    browse_workspace,
    project_workspace_root,
    read_workspace_file,
)
from app.routes.projects import _render_project_page

router = APIRouter()


@router.get(
    "/projects/{project_id}/files", response_class=HTMLResponse
)
def project_files_endpoint(
    project_id: int,
    request: Request,
    db: DB,
    path: str = ".",
) -> Response:
    """Render the project page with the Files tab active.

    ``path`` is a workspace-relative directory (default ``"."`` = root).
    Containment + missing-directory cases are handled by
    :func:`browse_workspace` and surface as in-page error text.

    Raises:
        HTTPException 404: When the project does not exist.
    """
    try:
        project = queries.get_project(db, project_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    listing = browse_workspace(project, path)
    return _render_project_page(
        request,
        db=db,
        project=project,
        active_tab="files",
        extra={
            "files_ctx": {"project": project, "listing": listing},
        },
    )


@router.get(
    "/projects/{project_id}/files/view", response_class=HTMLResponse
)
def project_file_view_endpoint(
    project_id: int,
    request: Request,
    db: DB,
    path: str,
) -> Response:
    """Render a single workspace file in the Files tab.

    Markdown is pre-rendered via the ``markdown`` library; other text renders
    as a ``<pre>`` block. Binary files show a "use Download" message instead
    of corrupted bytes.

    Raises:
        HTTPException 404: When the project does not exist.
    """
    try:
        project = queries.get_project(db, project_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    view = read_workspace_file(project, path)
    return _render_project_page(
        request,
        db=db,
        project=project,
        active_tab="files",
        extra={
            "files_ctx": {"project": project, "view": view},
        },
    )


@router.get("/projects/{project_id}/files/download")
def project_file_download_endpoint(
    project_id: int, db: DB, path: str
) -> Response:
    """Stream a workspace file to the browser as an attachment.

    Validates containment (rejects ``..`` traversal / absolute paths) and
    existence; ``Content-Disposition: attachment`` makes browsers save
    rather than inline.

    Raises:
        HTTPException 400: When file tools are not configured.
        HTTPException 404: When the project does not exist, the path escapes
            the workspace, or the file doesn't exist.
    """
    try:
        project = queries.get_project(db, project_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    root = project_workspace_root(project)
    if root is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "File tools not configured."
        )
    target = (root / path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.")
    return FileResponse(
        target,
        filename=target.name,
        media_type="application/octet-stream",
    )
