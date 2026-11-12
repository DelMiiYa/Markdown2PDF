"""Guard: mdtopdf_core is the SSOT consumed by other repos and must stay GUI-free."""

import ast
from pathlib import Path

FORBIDDEN = {"tkinter", "app", "ImageTk"}


def test_core_has_no_gui_imports() -> None:
    pkg = Path(__file__).resolve().parent.parent / "mdtopdf_core"
    for py in pkg.glob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
                names += [a.name for a in node.names]
            assert not FORBIDDEN & set(names), f"{py.name} imports GUI code: {names}"


def test_public_api_exports() -> None:
    import mdtopdf_core

    for name in mdtopdf_core.__all__:
        assert hasattr(mdtopdf_core, name)
