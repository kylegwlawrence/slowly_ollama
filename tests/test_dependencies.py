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
# (python-multipart imports as `python_multipart` since 0.0.13; the
# legacy `multipart` import name is deprecated.)
MODULES: list[str] = [
    "fastapi",
    "uvicorn",
    "httpx",
    "jinja2",
    "dotenv",
    "python_multipart",
    "pytest",
    "pytest_asyncio",
    "pytest_cov",
]


@pytest.mark.parametrize("module_name", MODULES)
def test_module_is_importable(module_name: str) -> None:
    """Importing the module succeeds.

    Args:
        module_name: Module name as it appears in `import` statements.
    """
    importlib.import_module(module_name)
