"""Windows PowerShell 5.1 reads a BOM-less .ps1 as the ANSI code page, so a
UTF-8 em dash or degree sign corrupts string literals and the script fails to
parse (the v0.32.0 deploy hit this). Every tracked script must be pure ASCII
or carry a UTF-8 BOM."""
import os
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOM = b"\xef\xbb\xbf"


def _tracked_scripts():
    out = subprocess.run(
        ["git", "ls-files", "*.ps1", "*.psm1", "*.bat", "*.cmd"],
        cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return [p for p in out.splitlines() if p]


def test_scripts_are_ascii_or_bom():
    scripts = _tracked_scripts()
    assert scripts, "git ls-files found no scripts; the check did not run"
    bad = []
    for rel in scripts:
        with open(os.path.join(ROOT, rel), "rb") as f:
            data = f.read()
        if data.startswith(BOM):
            if rel.lower().endswith((".bat", ".cmd")):
                bad.append(f"{rel}: BOM breaks cmd.exe")
            continue
        try:
            data.decode("ascii")
        except UnicodeDecodeError as e:
            bad.append(f"{rel}: non-ASCII at byte {e.start} and no UTF-8 BOM")
    assert not bad, "\n".join(bad)
