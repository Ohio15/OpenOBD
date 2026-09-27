"""Version is synchronized: the package version is the top CHANGELOG entry,
and the PyInstaller spec derives the exe version resource from the package."""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from openobd import __version__  # noqa: E402


def test_version_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)


def test_changelog_top_entry_matches_package():
    with open(os.path.join(ROOT, "CHANGELOG.md"), encoding="utf-8") as f:
        heads = re.findall(r"^## (\d+\.\d+\.\d+)", f.read(), re.M)
    assert heads and heads[0] == __version__


def test_spec_reads_version_from_package():
    with open(os.path.join(ROOT, "openobd.spec"), encoding="utf-8") as f:
        spec = f.read()
    assert "__version__" in spec and "version=version_info" in spec
    assert __version__ not in spec          # derived, never hardcoded
