# Source hints

Tool `description` fields for each data source. Paste into a function-calling
or MCP tool definition. Convention: corpus identity → routing signal → scope
limit, all in one tight paragraph.

---

## `search_arxiv`

Full-text chunks from arXiv STEM preprints (physics, math, CS, stats, bio, econ).
Use to find research, methods, or abstracts; many are abstract-only.

## `search_openalex`

Title/abstract chunks from the 5,000 most-cited academic works across all fields
(OpenAlex). Use for foundational research; biased toward older work.

## `search_factbook`

CIA World Factbook data for 260+ countries (geography, economy, government,
military). Country-level facts only; no sub-national breakdowns.

## `search_gutenberg`

Paragraph chunks of ~100 English public-domain books (Project Gutenberg). Use for
passages, quotes, or themes in pre-1928 classics; no modern texts.

## `search_simplewiki`

Simple English Wikipedia articles in plain language. Use for general facts,
definitions, biographies, or intro science; no events after early 2024.

## `search_pydocs`

Official Python 3 docs: stdlib, language reference, tutorials, what's-new. Use for
built-in modules, syntax, or behavior; no third-party packages.

## `search_wikihow`

Step-by-step wikiHow guides for everyday tasks (DIY, cooking, health,
relationships). Use for 'how do I…' instructions; not for factual reference.
