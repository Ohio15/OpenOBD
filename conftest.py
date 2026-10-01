"""Repo-wide pytest configuration.

* Qt runs on the offscreen platform so the suite needs no display (CI,
  the cortex stability observer, a locked session). Set here rather than on
  the command line because cmd.exe has no inline `VAR=value cmd` prefix.
* `--fail-on-skip` turns any skipped or xfailed test into a failing session,
  so a gate run cannot read a silently-skipped suite as green.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def pytest_addoption(parser):
    parser.addoption(
        "--fail-on-skip",
        action="store_true",
        default=False,
        help="fail the session if any test is skipped or xfailed",
    )


def pytest_sessionfinish(session, exitstatus):
    if not session.config.getoption("--fail-on-skip"):
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:
        return
    not_run = len(reporter.stats.get("skipped", [])) + len(reporter.stats.get("xfailed", []))
    if not_run and exitstatus == 0:
        reporter.write_line(
            f"--fail-on-skip: {not_run} test(s) skipped/xfailed; failing the session",
            red=True,
        )
        session.exitstatus = 1
