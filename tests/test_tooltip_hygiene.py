"""Tooltip hygiene (DR-032, brain 868953ce). QToolTip auto-detects rich text,
so a tooltip that interpolates runtime or file-derived text (a calibration
table name, a scalar note, a gauge label, a recording path) renders markup in
that text as HTML. Every such tooltip goes through tuningui.plain_tooltip.

The guard AST-scans openobd/ for setToolTip calls whose text argument is not
a string literal and not wrapped in plain_tooltip. A site whose argument is a
name or f-string that only ever carries literal text opts out with a
`# tooltip: literal` comment on a line of the call. Offscreen Qt, no sleeps."""
import ast
import os
import pathlib
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PKG = ROOT / "openobd"
OPT_OUT = "# tooltip: literal"
# Sites another open branch fixes; tolerated only while still unwrapped (a
# wrapped site is not reported, so the entry never fails once it lands).
# tuningui.ElidedLabel.setText -> plain_tooltip is PR #57 (feat/activetests-ux).
PENDING = {("tuningui.py", "ElidedLabel.setText")}


def _is_safe(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return True
    if isinstance(node, ast.Call):
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(
            func, "id", None)
        return name == "plain_tooltip"
    if isinstance(node, ast.IfExp):
        return _is_safe(node.body) and _is_safe(node.orelse)
    return False


def _violations(path: pathlib.Path) -> list[str]:
    src = path.read_text(encoding="utf-8")
    lines = src.splitlines()
    out: list[str] = []

    def walk(node: ast.AST, scope: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef)):
                walk(child, scope + [child.name])
                continue
            if (isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Attribute)
                    and child.func.attr == "setToolTip" and child.args):
                span = lines[child.lineno - 1:child.end_lineno]
                where = ".".join(scope)
                if (not _is_safe(child.args[-1])
                        and not any(OPT_OUT in ln for ln in span)
                        and (path.name, where) not in PENDING):
                    out.append(f"{path.name}:{child.lineno} ({where})")
            walk(child, scope)

    walk(ast.parse(src, filename=str(path)), [])
    return out


def test_every_fed_tooltip_goes_through_plain_tooltip():
    found = [v for p in sorted(PKG.rglob("*.py")) for v in _violations(p)]
    assert not found, (
        "setToolTip with non-literal text; wrap it in tuningui.plain_tooltip "
        f"or mark a literal-only site '{OPT_OUT}': {found}")


def test_the_guard_flags_an_unwrapped_runtime_tooltip(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text(
        "class W:\n"
        "    def f(self, name):\n"
        "        self.setToolTip(name)\n"
        "        self.setToolTip(f'{name} x')\n"
        "        item.setToolTip(0, name)\n"
        "        self.setToolTip(plain_tooltip(name))\n"
        "        self.setToolTip('lit' 'eral')\n"
        "        self.setToolTip(plain_tooltip(name) if name else '')\n"
        "        self.setToolTip(name)  # tooltip: literal\n",
        encoding="utf-8")
    assert _violations(bad) == ["bad.py:3 (W.f)", "bad.py:4 (W.f)",
                                "bad.py:5 (W.f)"]


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_gauge_label_tooltip_renders_markup_literally(qapp):
    from openobd.gauges import DialGauge
    g = DialGauge("rpm", "<b>x</b>", "rpm", 0, 7000)
    assert g.toolTip() == (
        "<qt>&lt;b&gt;x&lt;/b&gt; — click to reset min/max capture</qt>")
