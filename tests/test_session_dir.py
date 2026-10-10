"""Regression: the runner must create Pi's standard-store session dir
for the job cwd before spawning pi.

Pi aborts with ENOENT at startup when
~/.pi/agent/sessions/--<cwd-sanitized>-- does not exist (verified
2026-10-10).  runner.ensure_pi_session_dir pre-creates it, naming the
directory exactly the way pi's getDefaultSessionDirPath does.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pi_bridge import runner


def test_ensure_pi_session_dir_creates_dir(tmp_path, monkeypatch):
    """Sandbox HOME; the helper creates pi's dir; re-running does not fail."""
    home = tmp_path / "home"
    cwd = tmp_path / "proj"
    expected = home / ".pi" / "agent" / "sessions" / (
        "--" + str(cwd).lstrip("/").replace("/", "-") + "--")
    assert not expected.exists()

    d = runner.ensure_pi_session_dir(cwd, home=home)
    assert d == expected
    assert d.is_dir()

    # idempotent: a second call on an existing dir must not raise
    assert runner.ensure_pi_session_dir(cwd, home=home) == d
    assert d.is_dir()


def test_ensure_pi_session_dir_uses_process_home(tmp_path, monkeypatch):
    """Without an explicit home, Path.home() (i.e. $HOME) is the store root."""
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    d = runner.ensure_pi_session_dir(tmp_path / "work")
    assert d == (tmp_path / "fake-home" / ".pi"
                 / "agent" / "sessions"
                 / ("--" + str(tmp_path / "work").lstrip("/")
                    .replace("/", "-") + "--"))
    assert d.is_dir()


def test_pi_session_dir_matches_pi_naming():
    """Sanitization mirrors pi: leading '/' stripped, only / \\ : -> '-'.

    Characters pi does NOT replace (spaces, dots) must survive verbatim.
    """
    d = runner.pi_session_dir("/home/u/my.work dir", home="/h")
    assert d.name == "--home-u-my.work dir--"
    assert d == Path("/h/.pi/agent/sessions/--home-u-my.work dir--")
