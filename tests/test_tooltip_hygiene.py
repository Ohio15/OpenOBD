"""Tooltip hygiene (DR-032, brain 868953ce). QToolTip auto-detects rich text,
so a tooltip that interpolates runtime or file-derived text (a calibration
table name, a scalar note, a gauge label, a recording path) renders markup in
that text as HTML. Every such tooltip goes through tuningui.plain_tooltip.

The guard AST-scans openobd/ for tooltip sinks: setToolTip / setStatusTip /
setWhatsThis calls (positional or keyword text) and QToolTip.showText. The
text must be a string literal, a module-level string constant of the same
file, or a call to plain_tooltip that resolves to the tuningui import. A
sink method that is not called directly (an alias, a getattr by name, a
bound method passed around) is flagged, since the guard cannot see what it
is later fed. There is no opt-out comment and no exemption list.
Offscreen Qt, no sleeps."""
import ast
import os
import pathlib
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PKG = ROOT / "openobd"
SETTERS = {"setToolTip", "setStatusTip", "setWhatsThis"}
SINKS = SETTERS | {"showText"}
# Floor on sink calls in openobd/ so a broken scan (empty rglob, renamed
# package) cannot pass vacuously; there are well over this many today.
MIN_SITES = 25


def _module_str_constants(tree: ast.Module) -> set[str]:
    out = set()
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            out.add(node.targets[0].id)
    return out


def _plain_tooltip_is_tuningui(path: pathlib.Path, tree: ast.Module) -> bool:
    """plain_tooltip in this file is the tuningui one: tuningui.py itself
    (exactly one module-level def), or a `from .tuningui import` with no
    local def or other binding of the same name anywhere in the file."""
    defs = [n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef)) and n.name == "plain_tooltip"]
    if path.name == "tuningui.py":
        return len(defs) == 1 and defs[0] in tree.body
    if defs:
        return False
    imported = False
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            for a in n.names:
                if (a.asname or a.name) != "plain_tooltip":
                    continue
                if (n.module == "tuningui" and n.level == 1
                        and a.name == "plain_tooltip"):
                    imported = True
                else:
                    return False
        elif isinstance(n, ast.Import):
            if any(a.asname == "plain_tooltip" for a in n.names):
                return False
        elif (isinstance(n, ast.Name) and n.id == "plain_tooltip"
              and isinstance(n.ctx, ast.Store)):
            return False
    return imported


def _text_arg(call: ast.Call, name: str):
    """The text a sink call shows, or None when it cannot be located."""
    for kw in call.keywords:
        if kw.arg == "text":
            return kw.value
    if name == "showText":
        return call.args[1] if len(call.args) >= 2 else None
    return call.args[-1] if call.args else None


def _scan(path: pathlib.Path) -> tuple[list[str], int]:
    """(violations, number of sink calls) for one file."""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(path))
    consts = _module_str_constants(tree)
    plain_ok = _plain_tooltip_is_tuningui(path, tree)

    def safe(node) -> bool:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return True
        if isinstance(node, ast.Name):
            return node.id in consts
        if isinstance(node, ast.Call) and len(node.args) == 1:
            f = node.func
            if isinstance(f, ast.Name):
                return f.id == "plain_tooltip" and plain_ok
            if isinstance(f, ast.Attribute):
                return (f.attr == "plain_tooltip"
                        and isinstance(f.value, ast.Name)
                        and f.value.id == "tuningui")
            return False
        if isinstance(node, ast.IfExp):
            return safe(node.body) and safe(node.orelse)
        return False

    out: list[str] = []
    sites = 0

    def walk(node: ast.AST, scope: list[str]) -> None:
        nonlocal sites
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef)):
                walk(child, scope + [child.name])
                continue
            where = f"{path.name}:{getattr(child, 'lineno', '?')} " \
                    f"({'.'.join(scope)})"
            if isinstance(child, ast.Call):
                f = child.func
                if isinstance(f, ast.Attribute) and f.attr in SINKS:
                    sites += 1
                    if not safe(_text_arg(child, f.attr)):
                        out.append(where)
                    # The receiver may itself contain sinks; the func
                    # Attribute is accounted for, so skip it.
                    walk(f.value, scope)
                    for a in child.args:
                        walk(a, scope)
                    for kw in child.keywords:
                        walk(kw.value, scope)
                    continue
                if (isinstance(f, ast.Name) and f.id == "getattr"
                        and len(child.args) >= 2
                        and isinstance(child.args[1], ast.Constant)
                        and child.args[1].value in SINKS):
                    out.append(where + " getattr")
            elif isinstance(child, ast.Attribute) and child.attr in SINKS:
                # A sink not called in place: aliased, passed or stored.
                out.append(where + " alias")
            walk(child, scope)

    walk(tree, [])
    return out, sites


def _violations(path: pathlib.Path) -> list[str]:
    return _scan(path)[0]


def test_every_fed_tooltip_goes_through_plain_tooltip():
    files = sorted(PKG.rglob("*.py"))
    assert files, f"no sources under {PKG}"
    found: list[str] = []
    sites = 0
    for p in files:
        v, n = _scan(p)
        found += v
        sites += n
    assert sites >= MIN_SITES, (
        f"only {sites} tooltip sinks found under {PKG}; the scan is broken")
    assert not found, (
        "tooltip with non-literal text; wrap it in tuningui.plain_tooltip: "
        f"{found}")


def test_the_guard_flags_an_unwrapped_runtime_tooltip(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text(
        "from .tuningui import plain_tooltip\n"
        "HELP = 'static help'\n"
        "class W:\n"
        "    def f(self, name):\n"
        "        self.setToolTip(name)\n"
        "        self.setToolTip(f'{name} x')\n"
        "        item.setToolTip(0, name)\n"
        "        self.setToolTip(plain_tooltip(name))\n"
        "        self.setToolTip('lit' 'eral')\n"
        "        self.setToolTip(plain_tooltip(name) if name else '')\n"
        "        self.setToolTip(name)  # tooltip: literal\n"
        "        self.setToolTip(text=name)\n"
        "        self.setToolTip(text=plain_tooltip(name))\n"
        "        tip = self.setToolTip\n"
        "        getattr(self, 'setToolTip')(name)\n"
        "        QToolTip.showText(pos, name, self)\n"
        "        QToolTip.showText(pos, plain_tooltip(name), self)\n"
        "        QToolTip.showText(pos, text=name)\n"
        "        self.setStatusTip(name)\n"
        "        self.setWhatsThis(HELP)\n"
        "        self.setToolTip(HELP)\n",
        encoding="utf-8")
    assert _violations(bad) == [
        "bad.py:5 (W.f)", "bad.py:6 (W.f)", "bad.py:7 (W.f)",
        "bad.py:11 (W.f)", "bad.py:12 (W.f)", "bad.py:14 (W.f) alias",
        "bad.py:15 (W.f) getattr", "bad.py:16 (W.f)", "bad.py:18 (W.f)",
        "bad.py:19 (W.f)"]


def test_the_guard_rejects_a_local_plain_tooltip(tmp_path):
    """A same-named local helper is not the escaping one."""
    bad = tmp_path / "bad.py"
    bad.write_text(
        "def plain_tooltip(t):\n"
        "    return t\n"
        "def f(w, name):\n"
        "    w.setToolTip(plain_tooltip(name))\n",
        encoding="utf-8")
    assert _violations(bad) == ["bad.py:4 (f)"]
    other = tmp_path / "other.py"
    other.write_text(
        "from .elsewhere import plain_tooltip\n"
        "def f(w, name):\n"
        "    w.setToolTip(plain_tooltip(name))\n",
        encoding="utf-8")
    assert _violations(other) == ["other.py:3 (f)"]


def test_the_package_scan_counts_its_sites():
    """The floor is derived from the real tree, not assumed."""
    sites = sum(_scan(p)[1] for p in PKG.rglob("*.py"))
    assert sites >= MIN_SITES


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_gauge_label_tooltip_renders_markup_literally(qapp):
    from openobd.gauges import DialGauge
    g = DialGauge("rpm", "<b>x</b>", "rpm", 0, 7000)
    assert g.toolTip() == (
        "<qt>&lt;b&gt;x&lt;/b&gt; — click to reset min/max capture</qt>")


def test_action_chokepoints_escape_their_tip(qapp):
    """diagui._action / livedata._action / app._new_action wrap the tip, so
    a future runtime tip cannot inject markup (gate SC-16 residual)."""
    from PySide6.QtWidgets import QWidget
    from openobd import app, diagui, livedata
    host = QWidget()
    for fn in (diagui.DiagnosticsPage._action, livedata.LiveDataPage._action):
        a = fn(host, "open", "T", "<b>x</b>\nnext", lambda: None)
        assert a.toolTip() == "<qt>&lt;b&gt;x&lt;/b&gt;<br>next</qt>"
    a = app.MainWindow._new_action(host, "open", "T", "<i>y</i>", lambda: None,
                                   shortcut_hint="Ctrl+Y")
    assert a.toolTip() == "<qt>&lt;i&gt;y&lt;/i&gt;  (Ctrl+Y)</qt>"
