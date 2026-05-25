# Source hints

Tool `description` fields for each data source. Paste into a function-calling
or MCP tool definition. Convention: corpus identity → routing signal → scope
limit, all in one tight paragraph.

---

## `search_arxiv`

Full-text and abstract chunks from arXiv STEM preprints (physics, math, CS,
statistics, biology, economics). Use for recent or cutting-edge research, technical
methods, and quantitative findings; coverage skews post-2000 and many entries are
abstract-only.

## `search_openalex`

Title and abstract chunks from the 5,000 most-cited academic papers across all
disciplines (OpenAlex). Use for seminal or highly-cited foundational research;
coverage spans all fields including humanities and social science, but skews toward
pre-2010 work.

## `search_factbook`

CIA World Factbook statistics for 260+ countries (geography, economy, government,
demographics, military). Use for country-level facts, profiles, or cross-country
comparisons; no city, regional, or sub-national data.

## `search_gutenberg`

Paragraph-level chunks from ~100 English-language public-domain literary classics
via Project Gutenberg. Use to find passages, quotes, or thematic content from
pre-1928 fiction, poetry, or non-fiction; no modern or non-English texts.

## `search_simplewiki`

Simple English Wikipedia articles covering a broad range of topics written in plain
language. Use for general knowledge questions, definitions, biographies, or
introductory explanations on any subject; no events or updates after early 2024.

## `search_pydocs`

Official Python 3 documentation including the stdlib reference, language reference,
tutorials, and what's-new guides. Use for built-in modules, language syntax, or
standard-library behavior; does not cover third-party packages (e.g., NumPy,
Django, requests).

## `search_wikihow`

Step-by-step wikiHow guides for practical everyday tasks (DIY, cooking, health,
personal skills, relationships). Use when the query is procedural ('how to',
'steps to', 'how do I'); not suitable for factual, scientific, or reference
questions.
