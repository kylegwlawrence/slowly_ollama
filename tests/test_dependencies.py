"""Smoke test: every pinned dependency is importable.

Guards against a broken `pip install -r requirements.txt` — e.g. a yanked
version, a Python-version incompatibility, or a typo in the requirements
file. It does not validate library behaviour; later phases cover that.
"""

import importlib

import pytest

# Module (import) names. These differ from the distribution names in
# requirements.txt for two packages:
#   pytest-asyncio  -> pytest_asyncio
#   python-dotenv   -> dotenv
MODULES: list[str] = [
    "fastapi",
    "uvicorn",
    "httpx",
    "jinja2",
    "dotenv",
    "pytest",
    "pytest_asyncio",
]


@pytest.mark.parametrize("module_name", MODULES)
def test_module_is_importable(module_name: str) -> None:
    """Importing the module succeeds.

    Args:
        module_name: Module name as it appears in `import` statements.
    """
    importlib.import_module(module_name)
