"""Shared test setup: project paths and a way to use code that lives in notebooks."""
import ast
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
NOTEBOOKS = PROJECT_ROOT / "notebooks"

# Make profile_data.py (project root) and scripts/generate_analytics.py importable.
sys.path[:0] = [str(PROJECT_ROOT), str(PROJECT_ROOT / "scripts")]


def load_notebook_definitions(nb_path):
    """Run only the imports, functions and UPPER_CASE constants from a notebook's code cells.

    Cells that load data or write files are skipped, so this is fast and has no side effects.
    """
    ns = {"display": lambda *a, **k: None}
    for cell in json.loads(Path(nb_path).read_text(encoding="utf-8"))["cells"]:
        if cell["cell_type"] != "code":
            continue
        src = "".join(cell["source"]) if isinstance(cell["source"], list) else cell["source"]
        for node in ast.parse(src).body:
            is_constant = isinstance(node, ast.Assign) and all(
                isinstance(t, ast.Name) and t.id.isupper() for t in node.targets)
            if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef)) or is_constant:
                try:
                    exec(compile(ast.Module(body=[node], type_ignores=[]), str(nb_path), "exec"), ns)
                except (ImportError, NameError):  # needs data that only exists when the notebook runs
                    pass
    return ns


@pytest.fixture(scope="session")
def cleaning_defs():
    return load_notebook_definitions(NOTEBOOKS / "data_cleaning.ipynb")


@pytest.fixture(scope="session")
def warehouse_defs():
    return load_notebook_definitions(NOTEBOOKS / "load_warehouse.ipynb")
