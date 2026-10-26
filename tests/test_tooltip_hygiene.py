"""Tooltip hygiene (DR-032, brain 868953ce). Qt auto-detects rich text in
tooltips, so a tooltip that interpolates runtime or file-derived text (a
calibration table or axis name, a scalar note, a gauge label, a recording
path, a log channel) renders markup in that text as HTML. Every such tooltip
goes through plain_tooltip (defined in theme, re-exported by tuningui).

The guard AST-scans openobd/ for tooltip sinks:
- setToolTip / setStatusTip / setWhatsThis calls (positional or text=) and
  QToolTip.showText;
- setData / setItemData calls carrying a ToolTipRole argument in any
  position (model/combo (index, value, role), QStandardItem (value, role),
  QTableWidgetItem (role, value), QTreeWidgetItem (column, role, value)),
  `value=`, or a {ToolTipRole: value} roles dict;
- setProperty("toolTip", ...);
- `return` statements inside an `if role == ToolTipRole` / `is` /
  `role in (..., ToolTipRole)` block or a `case Qt.ToolTipRole:` arm (a
  model's data() / headerData() tooltip branch).
The shown text must be a string literal (or None for a role branch), a
module-level string constant of the same file, or a call to plain_tooltip
that resolves to theme / tuningui. A sink method not called in place (an
alias, a getattr by name, a bound method passed around) and any getattr
with a computed attribute name are flagged, since the guard cannot see
what they are fed. For the same reason any OTHER mention of ToolTipRole is
flagged: a role kept in a variable, a negated test (`role != ToolTipRole`,
whose tooltip return falls through), a ternary. Write the positive form.
No opt-out comment, no exemption list (brains 868953ce / 129af1c0).
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
DATA_SETTERS = {"setData", "setItemData"}
TIP_PROPERTIES = {"toolTip", "statusTip", "whatsThis"}
ROLE = "ToolTipRole"
# The escaping helper's home, and the module allowed to re-export it.
HOME, REEXPORT = "theme", "tuningui"
# Floor on sink sites in openobd/ so a broken scan (empty rglob, renamed
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


def _plain_tooltip_resolves(path: pathlib.Path, tree: ast.Module) -> bool:
    """A bare plain_tooltip in this file is the escaping one: theme.py
    defines it exactly once at module level; tuningui.py only re-exports it
    from .theme; any other file imports it from .theme or .tuningui and has
    no local def or other binding of the same name."""
    defs = [n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef)) and n.name == "plain_tooltip"]
    if path.stem == HOME:
        return len(defs) == 1 and defs[0] in tree.body
    if defs:
        return False
    allowed = {HOME} if path.stem == REEXPORT else {HOME, REEXPORT}
    imported = False
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            for a in n.names:
                if (a.asname or a.name) != "plain_tooltip":
                    continue
                if (n.module in allowed and n.level == 1
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


def _helper_modules(tree: ast.Module) -> set[str]:
    """Names bound by `from . import theme` / `from . import tuningui`, so
    theme.plain_tooltip(...) resolves to the real module. A name that is
    also bound any other way in the file (assigned, a parameter, a def, a
    different import) is not trusted: which binding is live at the call is
    not something the guard can see."""
    good: set[str] = set()
    other: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                bound = a.asname or a.name.split(".")[0]
                if (isinstance(n, ast.ImportFrom) and n.level == 1
                        and not n.module and a.name in (HOME, REEXPORT)
                        and bound == a.name):
                    good.add(bound)
                else:
                    other.add(bound)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                            ast.ClassDef)):
            other.add(n.name)
        elif isinstance(n, ast.Name) and isinstance(n.ctx,
                                                     (ast.Store, ast.Del)):
            other.add(n.id)
        elif isinstance(n, ast.arg):
            other.add(n.arg)
    return good - other


def _is_role(node) -> bool:
    return ((isinstance(node, ast.Attribute) and node.attr == ROLE)
            or (isinstance(node, ast.Name) and node.id == ROLE))


def _positive_role_test(test) -> bool:
    """`role == ToolTipRole`, `role is ToolTipRole`, `role in (...,
    ToolTipRole)`, or an and/or containing one: the body can run for a
    tooltip request. A negated test is NOT positive (its tooltip return is
    after the `if`), so its role mention is flagged instead."""
    if isinstance(test, ast.BoolOp):
        return any(_positive_role_test(v) for v in test.values)
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        op, left, right = test.ops[0], test.left, test.comparators[0]
        if isinstance(op, (ast.Eq, ast.Is)):
            return _is_role(left) or _is_role(right)
        if (isinstance(op, ast.In)
                and isinstance(right, (ast.Tuple, ast.List, ast.Set))):
            return any(_is_role(e) for e in right.elts)
    return False


def _text_arg(call: ast.Call, name: str):
    """The text a sink call shows, or None when it cannot be located."""
    for kw in call.keywords:
        if kw.arg == "text":
            return kw.value
    if name == "showText":
        return call.args[1] if len(call.args) >= 2 else None
    return call.args[-1] if call.args else None


def _role_values(call: ast.Call) -> list[tuple[ast.AST, object]]:
    """[(ToolTipRole node, value node or None)] for a setData / setItemData
    call. A positional role takes the argument after it when there is one
    (QTableWidgetItem (role, value), QTreeWidgetItem (column, role, value)),
    else the one before it (model / combo (index, value, role),
    QStandardItem (value, role)); `value=` wins; a {ToolTipRole: value}
    dict (QAbstractItemModel.setItemData) yields its value. None = the
    value cannot be located (flagged)."""
    args = call.args
    kw_value = next((k.value for k in call.keywords if k.arg == "value"),
                    None)
    out: list[tuple[ast.AST, object]] = []
    for kw in call.keywords:
        if kw.arg == "role" and _is_role(kw.value):
            out.append((kw.value, kw_value if kw_value is not None
                        else (args[-1] if args else None)))
    for i, a in enumerate(args):
        if _is_role(a):
            if kw_value is not None:
                value = kw_value
            elif i + 1 < len(args):
                value = args[i + 1]
            else:
                value = args[i - 1] if i >= 1 else None
            out.append((a, value))
        elif isinstance(a, ast.Dict):
            for k, v in zip(a.keys, a.values):
                if k is not None and _is_role(k):
                    out.append((k, v))
    return out


def _returns_in(stmts) -> list[ast.Return]:
    """Return statements in stmts, not descending into nested defs."""
    out = []
    stack = list(stmts)
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                          ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(n, ast.Return):
            out.append(n)
        stack.extend(ast.iter_child_nodes(n))
    return out


def _scan(path: pathlib.Path) -> tuple[list[str], int]:
    """(violations, number of sink sites) for one file."""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(path))
    consts = _module_str_constants(tree)
    plain_ok = _plain_tooltip_resolves(path, tree)
    modules = _helper_modules(tree)

    def safe(node, allow_none: bool = False) -> bool:
        if isinstance(node, ast.Constant):
            return (isinstance(node.value, str)
                    or (allow_none and node.value is None))
        if isinstance(node, ast.Name):
            return node.id in consts
        if isinstance(node, ast.Call) and len(node.args) == 1:
            f = node.func
            if isinstance(f, ast.Name):
                return f.id == "plain_tooltip" and plain_ok
            if isinstance(f, ast.Attribute):
                return (f.attr == "plain_tooltip"
                        and isinstance(f.value, ast.Name)
                        and f.value.id in modules)
            return False
        if isinstance(node, ast.IfExp):
            return (safe(node.body, allow_none)
                    and safe(node.orelse, allow_none))
        return False

    out: list[str] = []
    sites = 0
    # ToolTipRole nodes in a position the guard understands (a positive
    # test, a setData / setItemData role); any other mention is flagged.
    accounted: set[int] = set()
    scopes: dict[int, str] = {}

    def role_branch(stmts, scope: list[str]) -> None:
        nonlocal sites
        for ret in _returns_in(stmts):
            sites += 1
            if ret.value is not None and not safe(ret.value, True):
                out.append(f"{path.name}:{ret.lineno} "
                           f"({'.'.join(scope)}) role")

    def walk(node: ast.AST, scope: list[str]) -> None:
        nonlocal sites
        for child in ast.iter_child_nodes(node):
            scopes[id(child)] = ".".join(scope)
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef)):
                walk(child, scope + [child.name])
                continue
            where = f"{path.name}:{getattr(child, 'lineno', '?')} " \
                    f"({'.'.join(scope)})"
            if isinstance(child, ast.If) and _positive_role_test(child.test):
                accounted.update(id(n) for n in ast.walk(child.test)
                                 if _is_role(n))
                role_branch(child.body, scope)
            elif isinstance(child, ast.match_case):
                vals = [m.value for m in ast.walk(child.pattern)
                        if isinstance(m, ast.MatchValue)
                        and _is_role(m.value)]
                if vals:
                    accounted.update(id(v) for v in vals)
                    role_branch(child.body, scope)
            if isinstance(child, ast.Call):
                f = child.func
                if isinstance(f, ast.Attribute) and f.attr in SINKS:
                    sites += 1
                    if not safe(_text_arg(child, f.attr)):
                        out.append(where)
                    # The func Attribute is accounted for; scan the rest.
                    walk(f.value, scope)
                    for a in child.args:
                        walk(a, scope)
                    for kw in child.keywords:
                        walk(kw.value, scope)
                    continue
                if isinstance(f, ast.Attribute) and f.attr in DATA_SETTERS:
                    for role, value in _role_values(child):
                        sites += 1
                        accounted.add(id(role))
                        if not safe(value):
                            out.append(where + " role")
                if (isinstance(f, ast.Attribute) and f.attr == "setProperty"
                        and child.args
                        and isinstance(child.args[0], ast.Constant)
                        and child.args[0].value in TIP_PROPERTIES):
                    sites += 1
                    if len(child.args) < 2 or not safe(child.args[1]):
                        out.append(where + " property")
                if isinstance(f, ast.Name) and f.id == "getattr":
                    name = child.args[1] if len(child.args) >= 2 else None
                    if not (isinstance(name, ast.Constant)
                            and isinstance(name.value, str)):
                        out.append(where + " getattr")
                    elif name.value in SINKS:
                        out.append(where + " getattr")
            elif isinstance(child, ast.Attribute) and child.attr in SINKS:
                # A sink not called in place: aliased, passed or stored.
                out.append(where + " alias")
            walk(child, scope)

    walk(tree, [])

    def mentions(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if _is_role(child) and id(child) not in accounted:
                out.append(f"{path.name}:{child.lineno} "
                           f"({scopes.get(id(child), '')}) role-mention")
                continue
            mentions(child)

    mentions(tree)
    return out, sites


def _violations(path: pathlib.Path) -> list[str]:
    return _scan(path)[0]


def test_runtime_tooltips_go_through_plain_tooltip():
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
        "tooltip with non-literal text; wrap it in plain_tooltip: "
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


def test_the_guard_scans_roles_properties_and_computed_getattr(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text(
        "from . import theme\n"
        "class M:\n"
        "    def data(self, index, role):\n"
        "        if role == Qt.ToolTipRole:\n"
        "            return self.name\n"
        "        if role == Qt.ItemDataRole.ToolTipRole:\n"
        "            if index.row():\n"
        "                return theme.plain_tooltip(self.name)\n"
        "            return None\n"
        "        elif role in (Qt.DisplayRole, Qt.ToolTipRole):\n"
        "            return f'{self.name}'\n"
        "        if role == Qt.DisplayRole:\n"
        "            return self.name\n"
        "    def g(self, combo, item, name, attr):\n"
        "        combo.setItemData(0, name, Qt.ToolTipRole)\n"
        "        combo.setItemData(0, theme.plain_tooltip(name), Qt.ToolTipRole)\n"
        "        item.setData(Qt.ToolTipRole, name)\n"
        "        item.setData(Qt.DisplayRole, name)\n"
        "        self.setProperty('toolTip', name)\n"
        "        self.setProperty('toolTip', theme.plain_tooltip(name))\n"
        "        self.setProperty('objectName', name)\n"
        "        getattr(self, attr)(name)\n"
        "        getattr(self, '_x', None)\n",
        encoding="utf-8")
    assert _violations(bad) == [
        "bad.py:5 (M.data) role", "bad.py:11 (M.data) role",
        "bad.py:15 (M.g) role", "bad.py:17 (M.g) role",
        "bad.py:19 (M.g) property", "bad.py:22 (M.g) getattr"]


def test_the_guard_sees_every_role_position(tmp_path):
    """Gaps closed after gate pass 2 (brain 129af1c0): a role in the middle
    (QTreeWidgetItem.setData(col, role, value)), a roles dict, a role held
    in a variable, a negated test and a ternary were all invisible."""
    bad = tmp_path / "bad.py"
    bad.write_text(
        "from . import theme\n"
        "class M:\n"
        "    def g(self, item, model, idx, name):\n"
        "        item.setData(0, Qt.ToolTipRole, name)\n"
        "        item.setData(0, Qt.ToolTipRole, theme.plain_tooltip(name))\n"
        "        model.setItemData(idx, {Qt.ToolTipRole: name})\n"
        "        model.setItemData(idx, {Qt.ToolTipRole: 'lit', 0: name})\n"
        "        model.setData(idx, value=name, role=Qt.ToolTipRole)\n"
        "        role = Qt.ToolTipRole\n"
        "        model.setData(idx, name, role)\n"
        "    def data(self, index, role):\n"
        "        if role != Qt.ToolTipRole:\n"
        "            return None\n"
        "        return self.name\n"
        "    def headerData(self, s, o, role):\n"
        "        return self.name if role == Qt.ToolTipRole else None\n"
        "    def flags(self, role):\n"
        "        match role:\n"
        "            case Qt.ToolTipRole:\n"
        "                return self.name\n"
        "            case Qt.DisplayRole:\n"
        "                return self.name\n"
        "        if role is Qt.ToolTipRole and self.name:\n"
        "            return theme.plain_tooltip(self.name)\n",
        encoding="utf-8")
    assert _violations(bad) == [
        "bad.py:4 (M.g) role", "bad.py:6 (M.g) role", "bad.py:8 (M.g) role",
        "bad.py:20 (M.flags) role", "bad.py:9 (M.g) role-mention",
        "bad.py:12 (M.data) role-mention",
        "bad.py:16 (M.headerData) role-mention"]


def test_the_guard_does_not_trust_a_rebound_helper_module(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text(
        "from . import theme\n"
        "def f(w, name, theme=theme):\n"
        "    w.setToolTip(theme.plain_tooltip(name))\n",
        encoding="utf-8")
    assert _violations(bad) == ["bad.py:3 (f)"]


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
    aliased = tmp_path / "aliased.py"
    aliased.write_text(
        "from . import other as theme\n"
        "def f(w, name):\n"
        "    w.setToolTip(theme.plain_tooltip(name))\n",
        encoding="utf-8")
    assert _violations(aliased) == ["aliased.py:3 (f)"]


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


def test_model_tooltip_roles_render_file_text_literally(qapp):
    """Gate pass 2: axis label / unit (calibration file) and the cell
    tooltip (axis labels, unit, multi-line) are escaped on ToolTipRole."""
    from PySide6.QtCore import Qt
    from openobd.calspec import Axis, Table
    from openobd.model import CalTableModel
    vals = [[1.0, 2.0], [3.0, 4.0]]
    t = Table(name="T", unit="<i>u</i>", category="Spark/Main",
              x_axis=Axis("<b>X</b>", "rpm", [800, 1600]),
              y_axis=Axis("", "<s>kPa</s>", [20, 40]),
              values=[r[:] for r in vals], stock_values=[r[:] for r in vals])
    m = CalTableModel(t)
    h = m.headerData(0, Qt.Horizontal, Qt.ToolTipRole)
    assert h == "<qt>&lt;b&gt;X&lt;/b&gt;</qt>"
    v = m.headerData(0, Qt.Vertical, Qt.ToolTipRole)
    assert v.startswith("<qt>") and "<s>" not in v and "&lt;s&gt;" in v
    cell = m.data(m.index(0, 0), Qt.ToolTipRole)
    assert cell.startswith("<qt>") and cell.endswith("</qt>")
    assert "<i>" not in cell and "\n" not in cell
