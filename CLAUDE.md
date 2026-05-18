# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Local chat application for Mac (M3) that uses a locally-running Ollama instance as the inference engine — no cloud calls. See `docs/plans/PLAN.md` for the authoritative goal and phased build plan.

The project is in very early stages. Treat `docs/plans/PLAN.md` as the source of truth for what to build next; consult it before suggesting work.

## Working rules (from PLAN.md — follow these)

These are explicit preferences the user defined for this project. They override Claude defaults where they conflict:

- **Keep it simple first.** Add complexity only when needed; do not pre-build for hypothetical features.
- **Small commits, always ask before committing.** Never commit without explicit user approval, even for trivial changes.
- **Python style:** Google-style docstrings on functions/classes, type hints everywhere.
- **Write code comments.** Unlike Claude's default of minimal comments, this user wants inline comments explaining non-obvious code. Still avoid restating what well-named code already says.
- **Plans live in `docs/plans/`.** Any new plan document goes there.
- **Test after each phase.** Write tests for the phase's work and run them before moving on.
- **Audience:** the user is building their first full-stack app. Frame explanations accordingly — name tradeoffs, explain unfamiliar concepts, and challenge assumptions rather than silently accepting them (Phase 0 is explicitly a discussion/clarification phase).

## Phased build plan

The PLAN.md lays out work in ordered phases. Don't skip ahead — if asked to do work that belongs to a later phase, surface that and confirm before proceeding.

0. Discussion / requirements clarification (challenge assumptions, ask questions)
1. Package requirements
2. Database + schemas (SQLite)
3. Single shared database connection
4. Dataclasses + queries on top of the connection
5. Ollama client (calls Ollama's `/chat` endpoint)
6. FastAPI routers
7. Frontend (stack TBD — discuss with user before choosing)
8. Full test suite

## Tech stack

Locked in by the user:
- **Python** (3.13, see `.venv/pyvenv.cfg`) for as much as possible
- **FastAPI** as the backend framework
- **SQLite** for persistence
- **Ollama** local server as inference backend

Frontend stack and other choices are deliberately undecided — discuss before picking.

## Functional requirements (current scope)

- Chat window that calls Ollama's `/chat` endpoint
- Conversation history persists across app restarts
- Per-chat: model selection (the only per-chat setting)
- Global settings: `temperature`, `context size`

## Environment

- Virtualenv lives at `.venv/` (Python 3.13.13). Activate with `source .venv/bin/activate`.
- No `requirements.txt` / `pyproject.toml` yet — Phase 1 will define dependencies. If installing packages before then, add them to a requirements file as part of the same change.
- The repo is **not** yet a git repository. If commits are needed, initialize git first and confirm with the user.
