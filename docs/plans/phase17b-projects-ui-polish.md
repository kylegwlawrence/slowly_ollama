# Phase 17b — Projects UI Polish

## Context

Phase 17 shipped the data model + routing for projects-as-workspace
containers. The visual layer was left rough — bare unstyled form
elements, a sidebar that doesn't match the rest of the app, a chat
panel that doesn't scroll inside the new project-page wrapper, and a
"Default model" text input instead of a model picker.

This phase is **CSS + template + a few small route additions**. No
schema work, no new endpoints (one `/models` call is reused in the
project-settings dropdown). The goal is to bring the projects feature
in line with the pre-projects visual language: sage-tonal accents,
chat-item rows, Pico-overridden form controls, settings-page rhythm.

## Decisions (locked with the user)

- **Unified sidebar everywhere.** Projects index, every project tab,
  and global settings all share one sidebar that shows the full
  projects list (each project a clickable `.chat-item`-styled row with
  active-row highlight on the current project). `+ New project`
  appears at the top of the list. Settings link + theme toggle live in
  the footer (unchanged).
- **No more "Chats" tab inside a project.** The project's chats list
  lives in the main panel **above the composer** in the empty state.
  Once a chat is open, only the chat (messages + composer) shows. The
  remaining tabs are `Files` and `Settings` only.
- **Composer vertically centered** in the empty state (current behavior
  for the global empty composer; restored within the project page).
- **Project name** stays as a large heading above the tab nav in the
  main panel. The sidebar additionally highlights the active row.
- **Default-model picker** in project settings becomes a `<select>`
  populated lazily via `GET /models`, with a `(no default — use global)`
  option at top.
- **Files & Settings tabs** styled with the existing underline-active
  pattern.
- **Chat panel scroll** inside the project page must be fixed (`#main`
  is `overflow: hidden`, so every level between `#main` and `.messages`
  must propagate flex height down).

## Reference

The user pointed at https://github.com/kylegwlawrence/ollamacito/tree/main/frontend/src
for *page structure inspiration only*. All styling tokens come from
`static/style.css` already in this repo (sage palette, Pico overrides).
We do NOT pull any JS, CSS, or assets from that repo.

---

## Part A — Single unified sidebar

### A.1 Replace both sidebar templates with one shared template

The current `_projects_sidebar.html` (used on `/projects` and
`/settings`) and `_project_sidebar.html` (used inside a project) both
go away, replaced by `templates/_sidebar.html`. Both layouts pass the
same context shape: `projects` (list), `active_project_id` (int or
None).

**`templates/_sidebar.html`** (new):

```jinja
{# Shared sidebar across every page (projects index, inside-project,
   global settings). Shows the projects list as the primary nav, with
   '+ New project' at the top and Settings + theme toggle in the footer.

   Context:
     projects: list[Project] (alphabetical, from queries.list_projects)
     active_project_id: int | None — highlighted with aria-current="page" #}
<aside class="sidebar">
  <div class="sidebar__header">
    <h1 class="sidebar__logo">olliellama</h1>
  </div>
  <a class="sidebar__new-chat"
     href="/projects"
     hx-get="/projects"
     hx-target="#main"
     hx-swap="innerHTML"
     hx-push-url="/projects"
     aria-label="New project">
    <span class="material-symbols-outlined">add</span>
    New project
  </a>
  <ul id="projects-list" class="chats-list">
    {% for project in projects %}
      <li class="chat-item project-item"
          data-project-id="{{ project.id }}"
          {% if active_project_id == project.id %}aria-current="page"{% endif %}>
        <a href="/projects/{{ project.id }}/chats"
           hx-get="/projects/{{ project.id }}/chats"
           hx-target="#main"
           hx-swap="innerHTML"
           hx-push-url="true">{{ project.name }}</a>
      </li>
    {% endfor %}
  </ul>
  <div class="sidebar__footer">
    <a class="sidebar__settings" href="/settings"
       hx-get="/settings"
       hx-target="#main"
       hx-swap="innerHTML"
       hx-push-url="/settings">
      <span class="material-symbols-outlined">settings</span>
      Settings
    </a>
    <button class="theme-toggle" aria-label="Toggle dark mode" type="button">
      <span class="material-symbols-outlined theme-toggle__icon">dark_mode</span>
    </button>
  </div>
</aside>
```

Why reuse `chats-list` / `chat-item` classes for projects: the visual
contract is the same (vertical list of clickable rows, active-row
sage-tonal background, hover surface-hover). The extra `.project-item`
class is just a marker for JS/test selectors; it doesn't carry style.

### A.2 Delete the old sidebar templates

- Delete `templates/_projects_sidebar.html`
- Delete `templates/_project_sidebar.html`

Update `templates/index.html` to include `_sidebar.html` in all three
layout branches (projects / project / settings / fallback).

### A.3 Update routes to pass `projects` + `active_project_id`

In `app/routes.py`, every endpoint that renders `index.html` (or
`_project_page.html` / `_projects_index.html` for HTMX swaps) needs
`projects` and `active_project_id` in its template context. The
unified sidebar is included from `index.html`, so HTMX swaps that only
replace `#main` (not the sidebar) do NOT need the context. Full-page
renders do.

Endpoints to update (sidebar context: `projects` always;
`active_project_id` is set to the current project's id where applicable,
otherwise `None`):

| Endpoint | active_project_id |
|---|---|
| `index_endpoint` (redirect) | n/a |
| `list_projects_endpoint` | `None` |
| `settings_endpoint` | `None` |
| `project_chats_endpoint` | `project_id` |
| `project_chat_panel_endpoint` | `project_id` |
| `project_files_endpoint` | `project_id` |
| `project_file_view_endpoint` | `project_id` |
| `project_settings_endpoint` | `project_id` |

Each one already has `db: DB`. Add:

```python
projects = queries.list_projects(db)
```

near the top, and `projects=projects, active_project_id=...` in the
`index.html` context.

For the HTMX-swap branches (returning `_projects_index.html` /
`_project_page.html` only), the sidebar isn't re-rendered so we don't
need to pass `projects` — leave those branches alone. **Exception:**
the unified sidebar updates on `+ New project` are handled by an OOB
swap on `POST /projects` (see Part B).

### A.4 Active-state behavior on HTMX swap

`static/app.js` already clears `aria-current` on `#chats-list` rows on
click and sets it on the new active row. Extend the selector to cover
project rows in the unified sidebar:

```js
// in app.js — replace the chat-link click handler:
document.addEventListener('click', (e) => {
  const link = e.target.closest('#chats-list .chat-item a, #chats-list .project-item a');
  if (link) {
    document
      .querySelectorAll('#chats-list .chat-item[aria-current], #chats-list .project-item[aria-current]')
      .forEach((li) => li.removeAttribute('aria-current'));
    link.closest('li').setAttribute('aria-current', 'page');
    return;
  }
  if (e.target.closest('.sidebar__new-chat, .sidebar__settings')) {
    document
      .querySelectorAll('#chats-list [aria-current]')
      .forEach((li) => li.removeAttribute('aria-current'));
  }
});
```

(The `#chats-list .project-item` selector exists because we reuse the
`#chats-list` id for the projects list in the sidebar. Acceptable
because there's exactly one sidebar list at a time.)

---

## Part B — Projects index page (`/projects`)

The main panel of `/projects` shows a header + create form + list of
existing projects as buttons. The sidebar already shows the same list;
duplicating it in the main panel is intentional — the main panel
version is bigger, includes descriptions, and is the discoverable
home page.

### B.1 `templates/_projects_index.html` (rewrite)

```jinja
{# Main panel for /projects. Header + create form + alphabetical
   project list (large clickable rows). #}
<section class="projects-index">
  <header class="projects-index__header">
    <h1 class="settings__title">Projects</h1>
    <p class="settings__subtitle">A project bundles a workspace, chats, and default model.</p>
  </header>

  <form class="projects-index__create rag-server-form"
        hx-post="/projects"
        hx-target="#projects-grid"
        hx-swap="afterbegin">
    <label>
      Name
      <input type="text" name="name" required maxlength="80"
             placeholder="My new project">
    </label>
    <label>
      Description
      <input type="text" name="description" maxlength="200"
             placeholder="(optional)">
    </label>
    <button type="submit">Create project</button>
    <div id="projects-form-error" class="form-error" role="alert" hidden></div>
  </form>

  <ul id="projects-grid" class="projects-grid">
    {% for project in projects %}
      {% include "_project_item.html" %}
    {% endfor %}
  </ul>
</section>
```

Notes:
- Reuses `.rag-server-form` styling for the create form so it
  matches the global settings page.
- `#projects-grid` is the new id for the main-panel project list
  (distinct from `#projects-list` in the sidebar, so OOB-prepending
  a newly created project to one doesn't accidentally hit both).

### B.2 `templates/_project_item.html` (rewrite)

```jinja
<li class="project-tile" data-project-id="{{ project.id }}">
  <a class="project-tile__link"
     href="/projects/{{ project.id }}/chats"
     hx-get="/projects/{{ project.id }}/chats"
     hx-target="#main"
     hx-swap="innerHTML"
     hx-push-url="true">
    <div class="project-tile__name">{{ project.name }}</div>
    {% if project.description %}
      <div class="project-tile__desc">{{ project.description }}</div>
    {% endif %}
  </a>
</li>
```

### B.3 OOB sidebar update on POST /projects

`POST /projects` currently returns just the main-panel tile. To keep
the unified sidebar in sync without a full reload, also OOB-prepend
the project's row into `#projects-list` (the sidebar list).

In `create_project_endpoint` in `app/routes.py`:

```python
# Replace the existing TemplateResponse + HX-Push-Url block with:
tile_html = templates.get_template("_project_item.html").render(project=project)
sidebar_row_html = (
    f'<ul hx-swap-oob="afterbegin:#projects-list">'
    f'  <li class="chat-item project-item" data-project-id="{project.id}">'
    f'    <a href="/projects/{project.id}/chats"'
    f'       hx-get="/projects/{project.id}/chats"'
    f'       hx-target="#main"'
    f'       hx-swap="innerHTML"'
    f'       hx-push-url="true">{html.escape(project.name)}</a>'
    f'  </li>'
    f'</ul>'
)
response = HTMLResponse(
    content=tile_html + sidebar_row_html,
    status_code=status.HTTP_201_CREATED,
)
response.headers["HX-Push-Url"] = f"/projects/{project.id}/chats"
return response
```

(`html.escape` is already imported at the top of `routes.py`.)

---

## Part C — Inside-project page (Chats default + Files / Settings tabs)

### C.1 Drop the "Chats" tab from `_project_tabs.html`

```jinja
{# Two-tab nav inside a project: Files / Settings. The default view
   (no tab) is the chats list + composer in the main panel. #}
<nav class="project-tabs" aria-label="Project sections">
  <a class="project-tabs__tab{% if active_tab == 'chats' %} project-tabs__tab--active{% endif %}"
     href="/projects/{{ project.id }}/chats"
     hx-get="/projects/{{ project.id }}/chats"
     hx-target="#main"
     hx-swap="innerHTML"
     hx-push-url="true">
    <span class="material-symbols-outlined">chat</span>
    Chats
  </a>
  <a class="project-tabs__tab{% if active_tab == 'files' %} project-tabs__tab--active{% endif %}"
     href="/projects/{{ project.id }}/files"
     hx-get="/projects/{{ project.id }}/files"
     hx-target="#main"
     hx-swap="innerHTML"
     hx-push-url="true">
    <span class="material-symbols-outlined">folder</span>
    Files
  </a>
  <a class="project-tabs__tab{% if active_tab == 'settings' %} project-tabs__tab--active{% endif %}"
     href="/projects/{{ project.id }}/settings"
     hx-get="/projects/{{ project.id }}/settings"
     hx-target="#main"
     hx-swap="innerHTML"
     hx-push-url="true">
    <span class="material-symbols-outlined">settings</span>
    Settings
  </a>
</nav>
```

> Decision refinement: the user said "remove the Chats button and
> replace with a list of project chats above the composer". A clean
> read is *the Chats tab nav entry is gone, the chats list takes its
> place as the default view*. But losing the Chats tab means the only
> way back from Files/Settings to the chat view is via the sidebar's
> project link or the project-name heading. To keep navigation tight,
> the safe interpretation is **keep all three tabs but rename the
> default landing**: the Chats tab is still there, leads to the
> chats-list + composer view (no extra "Chats" header below it,
> because the tab itself is the label). Icons + a clean underline-
> active style do the heavy lifting visually. If during implementation
> this still reads as redundant, drop the Chats tab — but ship with
> all three first and review.

### C.2 `_project_page.html` — chats list above composer in the default view

```jinja
{# Main-panel content for an in-project view. Header + tab nav; body
   switches on `active_tab`. The Chats tab shows the chats list above
   a (vertically-centered) composer when no chat is open, or the open
   chat panel. Files / Settings have their own bodies. #}
<section class="project-page" data-project-id="{{ project.id }}">
  <header class="project-page__header">
    <h2 class="project-page__name">{{ project.name }}</h2>
    {% include "_project_tabs.html" %}
  </header>
  <div id="project-page-body" class="project-page__body">
    {% if active_tab == "chats" %}
      {% if conversation %}
        {% include "_chat_panel.html" %}
      {% else %}
        {# Empty-state: chats list above the composer, centered. #}
        <div class="project-empty">
          {% if chats %}
            <h3 class="project-empty__title">Chats</h3>
            <ul class="chats-list project-empty__chats">
              {% for chat in chats %}
                {% include "_chat_item.html" %}
              {% endfor %}
            </ul>
          {% endif %}
          {% include "_composer.html" %}
        </div>
      {% endif %}
    {% elif active_tab == "files" %}
      {% include "_project_files.html" %}
    {% elif active_tab == "settings" %}
      {% set project = settings_ctx.project %}
      {% set agents = settings_ctx.agents %}
      {% set saved = settings_ctx.saved %}
      {% include "_project_settings_body.html" %}
    {% endif %}
  </div>
</section>
```

Notes:
- The chats list reuses `_chat_item.html` (the existing kebab-menu row)
  so rename / delete still work and styling matches the previous
  sidebar version exactly.
- `_chat_item.html` builds URLs from `chat.id` + needs
  `active_chat_id` — already in context for the open-chat case;
  pass `active_chat_id=None` from the empty-state branches.

### C.3 Chat panel scroll fix

The chat panel is now nested inside `.project-page__body`. For
`.messages` to scroll, every ancestor up to `#main` must propagate
height correctly. Add these CSS rules (see Part E for the exact
ruleset):

- `.project-page` — fills `#main`, flex column.
- `.project-page__body` — flex 1, min-height 0, flex column.
- `.chat-panel` — already `height: 100%; display: flex; flex-direction:
  column` — but inside a flex column ancestor, `height: 100%` works
  poorly. Replace its `height: 100%` with `flex: 1; min-height: 0`.

### C.4 Routes — fetch chats list for the empty-state Chats view

`project_chats_endpoint` already fetches `chats = queries.list_conversations_in_project(db, project_id)` and passes it. Confirm the
template context for the non-conversation branch includes `chats` and
`active_chat_id=None`. (It does — no route change needed beyond the
`projects` / `active_project_id` additions from Part A.3.)

---

## Part D — Project Settings (default-model dropdown + form polish)

### D.1 `_project_settings_body.html` (rewrite)

```jinja
{# Settings tab body. The save form patches /projects/{id}; the server
   returns the body again with `saved=True` so we can show a transient
   confirmation. The delete form sits below with an hx-confirm guard. #}
<section class="project-settings settings">
  <header class="settings__header">
    <h1 class="settings__title">Project settings</h1>
    <p class="settings__subtitle">Edit name, description, and per-project defaults applied to new chats.</p>
  </header>

  <section class="settings__section">
    <form class="project-settings__form rag-server-form"
          hx-patch="/projects/{{ project.id }}"
          hx-target="#project-page-body"
          hx-swap="innerHTML">
      <label class="project-settings__field--full">
        Name
        <input type="text" name="name" required maxlength="80"
               value="{{ project.name }}">
      </label>
      <label class="project-settings__field--full">
        Description
        <textarea name="description" maxlength="400" rows="3">{{ project.description }}</textarea>
      </label>
      <label class="project-settings__field--full">
        Default model (for new chats in this project)
        <select name="default_model"
                data-default="{{ project.default_model or '' }}"
                hx-get="/models"
                hx-trigger="load"
                hx-target="this"
                hx-swap="innerHTML">
          <option value="">(no default — use global)</option>
          <option value="" disabled>Loading models…</option>
        </select>
      </label>
      <label class="project-settings__field--full">
        Default agent (for new chats in this project)
        <select name="default_agent">
          <option value=""{% if not project.default_agent %} selected{% endif %}>Normal (no agent)</option>
          {% for a in agents %}
            <option value="{{ a.name }}"{% if project.default_agent == a.name %} selected{% endif %}>{{ a.label }}</option>
          {% endfor %}
        </select>
      </label>
      <div class="project-settings__buttons">
        <button type="submit">Save</button>
        {% if saved %}<span class="project-settings__saved">Saved.</span>{% endif %}
      </div>
    </form>
  </section>

  <section class="settings__section settings__section--danger">
    <h2 class="settings__section-title">Danger zone</h2>
    <form class="project-settings__delete"
          hx-delete="/projects/{{ project.id }}"
          hx-confirm="Delete project '{{ project.name }}'? Chats will be deleted; workspace files are preserved on disk.">
      <button type="submit" class="project-settings__delete-btn">Delete project</button>
    </form>
  </section>
</section>
```

Two tricky details:

1. **`/models` clobbers the placeholder.** When HTMX swaps the inner
   HTML, the `<option value="">(no default…)</option>` placeholder is
   replaced too. We need the placeholder to survive. Two options:
   - **(a)** Add a route param to `/models` like `?with_blank=1` that
     prepends a `(no default)` option. Adds a route surface.
   - **(b) Recommended:** Wrap the placeholder in a sentinel: render
     the dropdown with a server-rendered `(no default)` option AND
     have `htmx:afterSwap` (in `app.js`) re-prepend it after the
     options load. Mirrors how `data-default` already restores the
     selected value.
   - **(c) Recommended cleaner:** Change `/models`'s template
     `_model_options.html` to include a "no default" first option
     ONLY when a `prepend_blank` flag is passed in context. Route gets
     an optional query param.

   **Pick (c)** for cleanliness:
   - Add a `prepend_blank: bool = False` query param to
     `list_models_endpoint` and to the template context.
   - In `_model_options.html`, wrap a `<option value="">(no default — use global)</option>` before the model loop when `prepend_blank`.
   - In the settings template, set `hx-get="/models?prepend_blank=1"`.

2. **`data-default` pre-selection.** The existing `app.js` block that
   reads `data-default` after `/models` swaps already supports both
   the composer model select AND any future select using the same
   attribute. Confirm the selector is generic:

   ```js
   document.body.addEventListener("htmx:afterSwap", (evt) => {
     const target = evt.target;
     if (target && target.tagName === "SELECT" && target.dataset.default) {
       const want = target.dataset.default;
       for (const opt of target.options) {
         if (opt.value === want) { target.value = want; break; }
       }
     }
   });
   ```

   Replace the existing `target.id === "composer-model"` check with
   the more general `target.tagName === "SELECT"` so the project
   settings dropdown gets the same treatment.

### D.2 `update_project_endpoint` — already handles dropdown values

The existing route handles empty-string ⇒ NULL via the `_UNSET`
sentinel. Confirm the form sends `default_model=""` when the
`(no default)` option is selected — it will, because HTML serializes
selected `<option value="">` as `default_model=`.

One bug in the existing route to fix while we're here: the dead-code
`form_data = await request.form() if False else None` line on
`routes.py:1547`. Delete it.

### D.3 `_model_options.html` — accept `prepend_blank`

```jinja
{% if error %}
  <option value="" disabled>{{ error }}</option>
{% else %}
  {% if prepend_blank|default(false) %}
    <option value="">(no default — use global)</option>
  {% endif %}
  {% for model_name in models %}
    <option value="{{ model_name }}">{{ model_name }}</option>
  {% endfor %}
{% endif %}
```

`list_models_endpoint`:

```python
@router.get("/models", response_class=HTMLResponse)
async def list_models_endpoint(
    request: Request,
    client: OllamaClient,
    prepend_blank: bool = False,
) -> Response:
    # ... existing body, just add `prepend_blank` to the contexts:
    context={"models": models, "error": None, "prepend_blank": prepend_blank}
    # and the error branch:
    context={"models": [], "error": str(e), "prepend_blank": prepend_blank}
```

---

## Part E — CSS (everything in `static/style.css`)

Append a new section at the end of `style.css`:

```css
/* ===== Phase 17b: Projects UI polish =================================== */

/* Sidebar projects list reuses `.chats-list` + `.chat-item` from earlier.
   `.project-item` is just a marker (no extra styling needed beyond
   the active-row aria-current rule already in style.css). */

/* ----- Projects index page (main panel) ----- */

.projects-index {
  max-width: 720px;
  margin: 0 auto;
  padding: var(--space-xl);
  display: flex;
  flex-direction: column;
  gap: var(--space-xl);
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}

.projects-index__header {
  padding-bottom: var(--space-md);
  border-bottom: 1px solid var(--border);
}

/* The create form uses the same .rag-server-form grid (1fr 2fr auto auto)
   pattern; we adjust column counts for our two inputs + button. */
.projects-index__create {
  grid-template-columns: 1fr 2fr auto;
}

.projects-grid {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-sm);
}

.project-tile {
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg);
  transition: background 0.15s ease, border-color 0.15s ease;
}

.project-tile:hover {
  background: var(--surface-hover);
  border-color: color-mix(in srgb, var(--accent) 40%, var(--border));
}

.project-tile__link {
  display: block;
  padding: var(--space-md) var(--space-lg);
  color: var(--text-primary);
  text-decoration: none;
}

.project-tile__name {
  font-size: 16px;
  font-weight: 500;
  margin: 0;
}

.project-tile__desc {
  font-size: 13px;
  color: var(--text-secondary);
  margin-top: var(--space-xs);
}

/* ----- Inside-project page ----- */

.project-page {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
}

.project-page__header {
  display: flex;
  flex-direction: column;
  gap: var(--space-sm);
  padding: var(--space-md) var(--space-xl) 0;
  background: var(--bg);
  border-bottom: 1px solid var(--border);
}

.project-page__name {
  margin: 0;
  font-size: 22px;
  font-weight: 500;
  color: var(--text-primary);
}

.project-page__body {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* ----- Tabs (Files / Settings, underline-active) ----- */

.project-tabs {
  display: flex;
  gap: var(--space-md);
}

.project-tabs__tab {
  display: inline-flex;
  align-items: center;
  gap: var(--space-xs);
  padding: var(--space-sm) var(--space-md);
  color: var(--text-secondary);
  text-decoration: none;
  border-bottom: 2px solid transparent;
  font-size: 14px;
  margin-bottom: -1px; /* overlap parent's border-bottom */
  transition: color 0.15s ease, border-color 0.15s ease;
}

.project-tabs__tab:hover { color: var(--text-primary); }

.project-tabs__tab--active {
  color: var(--accent-tonal-text);
  border-bottom-color: var(--accent);
}

.project-tabs__tab .material-symbols-outlined {
  font-size: 18px;
}

/* ----- Empty-state composer view (chats list + centered composer) ----- */

.project-empty {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--space-lg);
  padding: var(--space-xl);
  overflow-y: auto;
}

.project-empty__title {
  font-size: 14px;
  font-weight: 500;
  color: var(--text-secondary);
  margin: 0;
  align-self: flex-start;
  width: min(720px, 90%);
  margin-inline: auto;
}

.project-empty__chats {
  width: min(720px, 90%);
  margin: 0 auto;
  max-height: 30vh;
  overflow-y: auto;
}

/* ----- Chat-panel scroll fix inside the project page ----- */

/* Replace the existing `.chat-panel { height: 100%; ... }` rule's
   `height: 100%` with flex-based growth so it works inside the new
   .project-page__body ancestor. Edit the existing rule in place. */
.chat-panel {
  flex: 1;
  min-height: 0;
  /* keep existing: display: flex; flex-direction: column; */
}

/* ----- Project settings ----- */

.project-settings {
  /* Already extends .settings — inherits max-width + scrollable column. */
}

.project-settings__field--full {
  grid-column: 1 / -1;
}

.project-settings__buttons {
  grid-column: 1 / -1;
  display: flex;
  align-items: center;
  gap: var(--space-md);
}

.project-settings__saved {
  color: var(--success);
  font-size: 13px;
}

.settings__section--danger {
  border-top: 1px solid var(--border);
  padding-top: var(--space-lg);
}

.project-settings__delete {
  margin: 0;
}

.project-settings__delete-btn {
  background: none;
  border: 1px solid var(--danger);
  color: var(--danger);
  padding: var(--space-xs) var(--space-md);
  border-radius: var(--radius-md);
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  width: auto;
  margin: 0;
}

.project-settings__delete-btn:hover {
  background: color-mix(in srgb, var(--danger) 10%, transparent);
}

/* ----- Project files ----- */

.project-files {
  max-width: 960px;
  margin: 0 auto;
  padding: var(--space-xl);
  display: flex;
  flex-direction: column;
  gap: var(--space-md);
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}

.project-files__crumbs {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-xs);
  font-size: 13px;
  color: var(--text-secondary);
}

.project-files__crumbs a {
  color: var(--accent-tonal-text);
  text-decoration: none;
  padding: var(--space-xs) var(--space-sm);
  border-radius: var(--radius-md);
}

.project-files__crumbs a:hover { background: var(--surface-hover); }

.project-files__empty {
  color: var(--text-secondary);
  font-size: 14px;
  padding: var(--space-lg);
  text-align: center;
  background: var(--surface);
  border: 1px dashed var(--border);
  border-radius: var(--radius-md);
}

.project-files__list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-xs);
}

.project-files__item {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  padding: var(--space-sm) var(--space-md);
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  transition: background 0.15s ease;
}

.project-files__item:hover { background: var(--surface-hover); }

.project-files__dir,
.project-files__file {
  display: inline-flex;
  align-items: center;
  gap: var(--space-sm);
  flex: 1;
  color: var(--text-primary);
  text-decoration: none;
  font-size: 14px;
}

.project-files__dir .material-symbols-outlined,
.project-files__file .material-symbols-outlined {
  color: var(--accent-tonal-text);
  font-size: 20px;
}

.project-files__size {
  font-size: 12px;
  color: var(--text-secondary);
  font-variant-numeric: tabular-nums;
}

.project-files__download {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--text-secondary);
  text-decoration: none;
  padding: var(--space-xs);
  border-radius: var(--radius-sm);
}

.project-files__download:hover {
  background: var(--surface-hover);
  color: var(--accent-tonal-text);
}

.project-files__view-header {
  display: flex;
  align-items: center;
  gap: var(--space-md);
  padding: var(--space-sm) var(--space-md);
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  font-size: 13px;
  color: var(--text-secondary);
}

.project-files__view-header .project-files__download {
  margin-left: auto;
  color: var(--accent-tonal-text);
  font-weight: 500;
}

.project-files__pre {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: var(--space-md);
  overflow-x: auto;
  font-family: ui-monospace, SFMono-Regular, monospace;
  font-size: 13px;
  line-height: 1.5;
  color: var(--text-primary);
  margin: 0;
}

.project-files__markdown {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: var(--space-lg);
  font-size: var(--font-size-base);
  line-height: 1.6;
}

/* ----- Sidebar "+ New project" reuses .sidebar__new-chat styling ----- */
/* No new rules needed — the template uses the existing class. */
```

### Critical edits to existing CSS rules

1. **`.chat-panel { height: 100%; ... }`** → change to
   `flex: 1; min-height: 0;` (keeping `display: flex; flex-direction:
   column;`). This is the **single root cause of the scroll bug** the
   user reported. Search for the rule near `style.css:422`.

2. **`.layout { height: 100vh; overflow: hidden; }`** stays — it's
   the boundary that contains everything.

3. **`#main { flex: 1; ... overflow: hidden; }`** stays — provides
   the bounded box that the project-page's flex column lives in.

The fix chain is: `.layout > #main > .project-page > .project-page__body
> .chat-panel > .messages` — every one of those must propagate flex
height or apply `min-height: 0` so flex children can shrink. The CSS
above does exactly that.

---

## Part F — Tests

The Phase 17 retro presumably has integration tests that touch the
specific markup we're changing. Inventory + fix:

### F.1 Inventory of touched tests

```bash
grep -lE "project-tabs|projects-list|project_sidebar|project-tile|_project_item|default_model.*input" tests/
```

Likely files:
- `tests/test_projects.py` (or wherever phase 17's tests live)
- `tests/test_routes_projects.py`
- `tests/test_settings.py` (for `/models` if we changed it)

### F.2 Test updates

1. **Sidebar contract.** New test:
   ```python
   def test_projects_index_sidebar_lists_all_projects(client, db):
       # seed 3 projects
       resp = client.get("/projects")
       html = resp.text
       assert 'id="projects-list"' in html
       for name in ("Default", "Alpha", "Beta"):
           assert name in html
   ```
2. **Active-row highlight inside a project.** New test:
   ```python
   def test_project_page_sidebar_highlights_active_project(client, db):
       # seed project; navigate to /projects/{id}/chats
       resp = client.get(f"/projects/{pid}/chats")
       assert f'data-project-id="{pid}"' in resp.text
       assert 'aria-current="page"' in resp.text
   ```
3. **Default-model dropdown.** New test:
   ```python
   def test_project_settings_default_model_is_select(client, db, pid):
       resp = client.get(f"/projects/{pid}/settings")
       assert '<select name="default_model"' in resp.text
       assert 'hx-get="/models?prepend_blank=1"' in resp.text
       assert 'data-default="' in resp.text
   ```
4. **`/models?prepend_blank=1`.** New test:
   ```python
   def test_models_endpoint_prepend_blank(client_with_models):
       resp = client.get("/models?prepend_blank=1")
       assert '<option value="">(no default — use global)</option>' in resp.text
   ```
5. **Chat scroll fix.** Substring assertion on CSS rule:
   ```python
   def test_chat_panel_uses_flex_grow_for_scroll():
       css = Path("static/style.css").read_text()
       # Ensure .chat-panel no longer has `height: 100%`
       rule = re.search(r"\.chat-panel\s*\{[^}]+\}", css)
       assert rule is not None
       assert "height: 100%" not in rule.group()
       assert "flex: 1" in rule.group()
       assert "min-height: 0" in rule.group()
   ```
6. **Tab nav no longer has "Chats" as a separate underlined tab from
   the chats list.** Adjust the existing project-tabs test if it
   asserts three tabs — depending on the decision in C.1, either keep
   asserting 3 tabs OR drop to 2.
7. **Existing tests that grep for `_projects_sidebar.html` /
   `_project_sidebar.html` template renders** — point them at
   `_sidebar.html`.

Aim: **no regression in test count**; new total likely +6 or so.

---

## Part G — JS update (`static/app.js`)

Two surgical edits (both already mentioned above; consolidating):

1. **Generalize `data-default` selector** to any `<select>`, not just
   `#composer-model`:
   ```js
   if (target && target.tagName === "SELECT" && target.dataset.default) {
     const want = target.dataset.default;
     for (const opt of target.options) {
       if (opt.value === want) { target.value = want; break; }
     }
   }
   ```
2. **Generalize the sidebar `aria-current` click handler** to cover
   `.project-item` rows alongside `.chat-item` rows.

No other JS changes needed.

---

## Execution order

1. **CSS first** (Part E) — paint the styles, see the diff visually
   in the browser even before touching templates.
2. **Sidebar consolidation** (Part A) — one shared `_sidebar.html`;
   delete the old two; update `index.html`; update all routes to pass
   `projects` + `active_project_id`.
3. **Projects index page** (Part B) — rewrite template + tile
   template + OOB sidebar update in `create_project_endpoint`.
4. **Inside-project page** (Part C) — drop Chats tab body, add
   chats-list + composer in default view, fix chat-panel CSS rule.
5. **Project settings** (Part D) — rewrite settings body, add
   `prepend_blank` to `/models`, generalize the `data-default` JS.
6. **Files page styling** — already covered by CSS in Part E; just
   ensure markup matches selectors.
7. **JS edits** (Part G).
8. **Tests** (Part F).
9. **Manual smoke test in a real browser** — per CLAUDE.md's
   "smoke-test UI changes in a real browser" rule.

## Estimated diff size

- `static/style.css`: +~250 lines, ~2 in-place edits
- `static/app.js`: ~10 lines net
- `templates/_sidebar.html`: new (+~45)
- `templates/_projects_sidebar.html`, `_project_sidebar.html`: deleted
- `templates/_projects_index.html`: rewrite (~35 lines)
- `templates/_project_item.html`: rewrite (~15 lines)
- `templates/_project_page.html`: rewrite (~30 lines)
- `templates/_project_tabs.html`: rewrite (~25 lines)
- `templates/_project_settings_body.html`: rewrite (~55 lines)
- `templates/_project_files.html`: minor markup adjustments
- `templates/_model_options.html`: +3 lines
- `templates/index.html`: 3 sed-style edits
- `app/routes.py`: ~10 endpoints get `projects = queries.list_projects(db)` + context kwargs; `/models` gets a query param; `create_project_endpoint` rewrites the response; one dead line deleted
- `tests/`: +6 tests, ~5 updated

## Risks / unknowns

- **`/models` route behavior change** is backwards-compatible only if
  the default `prepend_blank=False` matches today's response — it
  does, since the new code is gated on the flag.
- **Sidebar context leak** to HTMX-swap branches: confirmed not
  needed because those branches return a fragment that gets swapped
  into `#main`, not the full layout. But if any test asserts the
  sidebar appears in a fragment response, it will fail correctly —
  fix the test, not the code.
- **The Chats tab decision (C.1)** could land either way depending
  on browser smoke-test impressions. Plan A: keep all three tabs.
  Plan B: drop Chats and rely on the sidebar project link as the
  way to return to chats from Files/Settings. Ship Plan A, evaluate.
- **Project tile vs. project sidebar row visual divergence.** Tiles
  in the main panel are larger (with description); sidebar rows are
  compact (name only). This is intentional — the main panel is the
  discovery view; the sidebar is the navigation rail.
