"""_deploy.ps1 pre-deploy gate (discovery 455efc83).

Runs the real _deploy.ps1 under Windows PowerShell against a throwaway
package dir, dist exe, build.log and install dir (all passed as parameters,
with -NoShortcuts), so the real install dir and the user's Start menu and
Desktop are never touched. The "exes" are copies of a system exe stamped with
a real Windows version resource, so ProductVersion is read the way the
production script reads it.
"""
import os
import re
import shutil
import subprocess
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEPLOY = os.path.join(ROOT, "_deploy.ps1")
BUILD_BAT = os.path.join(ROOT, "_build.bat")
POWERSHELL = shutil.which("powershell")
SEED_EXE = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                        "System32", "whoami.exe")

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or not POWERSHELL or not os.path.exists(SEED_EXE),
    reason="_deploy.ps1 needs Windows PowerShell")

GOOD_LOG = "...pyinstaller output...\nBUILD_DONE_EXIT_0\nVERIFY_DONE_EXIT_0\n"


def _stamp(path, version):
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo, StringFileInfo, StringStruct, StringTable,
        VarFileInfo, VarStruct, VSVersionInfo, write_version_info_to_executable)
    vt = tuple(int(x) for x in version.split(".")) + (0,)
    info = VSVersionInfo(
        ffi=FixedFileInfo(filevers=vt, prodvers=vt),
        kids=[StringFileInfo([StringTable("040904B0", [
            StringStruct("FileVersion", version),
            StringStruct("ProductName", "OpenOBD"),
            StringStruct("ProductVersion", version)])]),
            VarFileInfo([VarStruct("Translation", [1033, 1200])])])
    write_version_info_to_executable(str(path), info)


def _make_exe(path, version):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    shutil.copyfile(SEED_EXE, path)
    _stamp(path, version)


def _product_version(path):
    out = subprocess.run(
        [POWERSHELL, "-NoProfile", "-Command",
         f"(Get-Item '{path}').VersionInfo.ProductVersion"],
        capture_output=True, text=True, check=True)
    return out.stdout.strip()


class Sandbox:
    def __init__(self, root, pkg_version="1.2.3", exe_version="1.2.3"):
        self.root = str(root)
        self.pkg = os.path.join(self.root, "openobd")
        self.dist = os.path.join(self.root, "dist", "openobd.exe")
        self.log = os.path.join(self.root, "build.log")
        self.install = os.path.join(self.root, "install")
        os.makedirs(os.path.join(self.pkg, "__pycache__"))
        with open(os.path.join(self.pkg, "__init__.py"), "w") as f:
            f.write(f'__version__ = "{pkg_version}"\n')
        with open(os.path.join(self.pkg, "mod.py"), "w") as f:
            f.write("X = 1\n")
        _make_exe(self.dist, exe_version)
        self.write_log(GOOD_LOG)
        # Sources 60 s older than the build; log and exe written together.
        now = time.time()
        for name in ("__init__.py", "mod.py"):
            os.utime(os.path.join(self.pkg, name), (now - 60, now - 60))
        self.set_times(exe=now - 5, log=now)

    def write_log(self, text):
        with open(self.log, "w", newline="") as f:
            f.write(text)

    def set_times(self, exe, log):
        os.utime(self.dist, (exe, exe))
        os.utime(self.log, (log, log))

    def install_old(self, version="1.0.0"):
        _make_exe(os.path.join(self.install, "OpenOBD.exe"), version)

    def deploy(self):
        return subprocess.run(
            [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
             DEPLOY, "-DistExe", self.dist, "-InstallDir", self.install,
             "-BuildLog", self.log, "-PackageDir", self.pkg, "-NoShortcuts"],
            capture_output=True, text=True, timeout=120)

    def installed(self):
        return os.path.join(self.install, "OpenOBD.exe")


def _assert_refused(sb, result, fragment):
    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert "deploy REFUSED" in out
    assert fragment in out
    assert not os.path.isdir(os.path.join(sb.install, "backup"))


def test_matching_build_is_deployed_with_backup(tmp_path):
    sb = Sandbox(tmp_path)
    sb.install_old("1.0.0")
    r = sb.deploy()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "pre-deploy gate passed" in r.stdout
    assert _product_version(sb.installed()) == "1.2.3"
    backup = os.path.join(sb.install, "backup", "OpenOBD-1.0.0.exe")
    assert os.path.isfile(backup)
    assert _product_version(backup) == "1.0.0"


def test_first_install_without_existing_exe(tmp_path):
    sb = Sandbox(tmp_path)
    r = sb.deploy()
    assert r.returncode == 0, r.stdout + r.stderr
    assert _product_version(sb.installed()) == "1.2.3"


def test_version_mismatch_refused_and_install_untouched(tmp_path):
    # The incident: a 0.32.2 exe sitting in dist while the package is 0.33.0.
    sb = Sandbox(tmp_path, pkg_version="1.3.0", exe_version="1.2.2")
    sb.install_old("1.2.9")
    r = sb.deploy()
    _assert_refused(sb, r, "v1.2.2 but the package is v1.3.0")
    assert _product_version(sb.installed()) == "1.2.9"


def test_stale_build_older_than_sources_refused(tmp_path):
    sb = Sandbox(tmp_path)
    sb.install_old("1.0.0")
    newer = time.time() + 30
    nested = os.path.join(sb.pkg, "sub")
    os.makedirs(nested)
    path = os.path.join(nested, "late.py")
    with open(path, "w") as f:
        f.write("Y = 2\n")
    os.utime(path, (newer, newer))
    r = sb.deploy()
    _assert_refused(sb, r, "was modified after")
    assert _product_version(sb.installed()) == "1.0.0"


def test_pycache_newer_than_exe_is_ignored(tmp_path):
    sb = Sandbox(tmp_path)
    newer = time.time() + 30
    path = os.path.join(sb.pkg, "__pycache__", "mod.cpython-312.pyc")
    with open(path, "wb") as f:
        f.write(b"x")
    os.utime(path, (newer, newer))
    r = sb.deploy()
    assert r.returncode == 0, r.stdout + r.stderr


def test_exe_from_a_different_build_than_the_log_refused(tmp_path):
    # Same version, sources older than the exe, clean log - but the log was
    # written an hour after the exe, so the exe is not from that build.
    sb = Sandbox(tmp_path)
    now = time.time()
    for name in ("__init__.py", "mod.py"):
        os.utime(os.path.join(sb.pkg, name), (now - 7200, now - 7200))
    sb.set_times(exe=now - 3600, log=now)
    r = sb.deploy()
    _assert_refused(sb, r, "was not written by the build recorded in")


@pytest.mark.parametrize("log, fragment", [
    ("output\n", "no BUILD_DONE_EXIT line"),
    ("BUILD_DONE_EXIT_1\n", "non-zero"),
    ("BUILD_DONE_EXIT_0\n", "no VERIFY_DONE_EXIT line"),
    ("BUILD_DONE_EXIT_0\nVERIFY_DONE_EXIT_4\n", "VERIFY_DONE_EXIT_4 is non-zero"),
    ("VERIFY_DONE_EXIT_0\nBUILD_DONE_EXIT_0\n", "precedes"),
])
def test_bad_build_log_refused(tmp_path, log, fragment):
    sb = Sandbox(tmp_path)
    sb.install_old("1.0.0")
    sb.write_log(log)
    now = time.time()
    sb.set_times(exe=now - 5, log=now)
    r = sb.deploy()
    _assert_refused(sb, r, fragment)
    assert _product_version(sb.installed()) == "1.0.0"


def test_missing_build_log_refused(tmp_path):
    sb = Sandbox(tmp_path)
    os.remove(sb.log)
    r = sb.deploy()
    _assert_refused(sb, r, "no build log")
    assert not os.path.exists(sb.install)


def test_missing_dist_exe_refused(tmp_path):
    sb = Sandbox(tmp_path)
    os.remove(sb.dist)
    r = sb.deploy()
    _assert_refused(sb, r, "no build at")
    assert not os.path.exists(sb.install)


def test_build_bat_deletes_previous_exe_before_building():
    # A failed or skipped build must never leave a stale exe behind. Static
    # check: a real build needs PyInstaller and minutes.
    with open(BUILD_BAT, encoding="utf-8") as f:
        text = f.read()
    delete = re.search(r'del /f /q "dist\\openobd\.exe"', text)
    pyi = re.search(r"-m PyInstaller", text)
    assert delete and pyi and delete.start() < pyi.start()
