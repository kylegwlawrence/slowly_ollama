# Phase 12h retrospective — expandable tool-row sources

*(stub — fill in after the implementation has lived long enough to
generate observations about what went well and what didn't.)*

## Scope

Add an expandable per-row sources list inside the aggregated tool
card (shipped in 12e). When a `query_rag` call returns results, the
row gains a chevron; clicking it reveals the deduplicated source
titles (with section for single-chunk entries, chunk count for
multi-chunk). Non-RAG tools (`current_time`, future read-only tools
without sources) keep the original plain-row layout.

Locked design constraints (decided with user during planning):
- Title + section only — no chapter, no page, no URL, no snippet.
- Dedup by title; multi-chunk drops the section in favor of a count.
- Single-chunk with section shows `Title (§Section)`.
- Storage as a JSON envelope on `tool_result.content`; pre-12h plain
  text decodes through a fallback so legacy conversations still
  render (as plain non-expandable rows).
- No SQL migration.

## What landed

| Commit | Title |
|---|---|
| *(TBD)* | docs: plan phase 12h — tool-row source list |
| *(TBD)* | feat: phase 12h — expandable per-row sources on tool card |

Tests: 297/297 passing at phase close (added 6 tool tests, 12 render
tests, 14 template tests, 5 generation tests, 2 route tests). Coverage
97% on `app/` + `main.py` — the misses are pre-existing branches in
unrelated code (Ollama error paths, dict.get fallbacks).

## What went well

*(fill in after living with the change)*

## What surprised us

*(fill in)*

## Notes for future phases

*(fill in)*
