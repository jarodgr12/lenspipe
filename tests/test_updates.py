"""2.4.2: version check, update and restart from the console."""

from __future__ import annotations

import sys

import pytest

from lenspipe import __version__
from lenspipe.updates import (
    UpdateStatus,
    check_for_update,
    is_newer,
    parse_release_tags,
    restart_argv,
)

LS_REMOTE = """\
0123abcd\trefs/tags/v2.3.5
4567abcd\trefs/tags/v2.4.0
89abcdef\trefs/tags/v2.4.1
deadbeef\trefs/tags/v2.10.0
cafebabe\trefs/tags/not-a-release
"""


def test_release_tags_sort_numerically_and_ignore_other_tags() -> None:
    assert parse_release_tags(LS_REMOTE) == ["2.3.5", "2.4.0", "2.4.1", "2.10.0"]
    assert parse_release_tags("") == []
    assert is_newer("2.10.0", "2.4.1") and not is_newer("2.4.1", "2.4.1") and not is_newer("2.3.9", "2.4.0")


def test_update_status_summary_and_flags() -> None:
    newer = UpdateStatus(installed="2.4.1", latest="2.4.2", source="lenspipe v2.4.1 (/x)")
    assert newer.update_available and newer.can_update and "2.4.2 is available" in newer.summary()
    current = UpdateStatus(installed="2.4.2", latest="2.4.2", source="lenspipe v2.4.2 (/x)")
    assert not current.update_available and "newest release" in current.summary()
    unknown = UpdateStatus(installed="2.4.2", latest=None, source=None)
    assert not unknown.update_available and not unknown.can_update
    assert "could not be checked" in unknown.summary() and "development checkout" in unknown.summary()


def test_check_for_update_reads_git_and_uv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("lenspipe.updates.latest_release", lambda repo=None, refresh=False: "99.0.0")
    monkeypatch.setattr("lenspipe.updates.install_source", lambda: None)
    status = check_for_update()
    assert status.installed == __version__ and status.latest == "99.0.0" and status.update_available


def test_restart_argv_reuses_the_ui_arguments_without_opening_a_browser() -> None:
    assert restart_argv(["ui", "/p", "--port", "8972"]) == [sys.executable, "-m", "lenspipe", "ui", "/p", "--port", "8972", "--no-browser"]
    assert restart_argv(["ui", "/p", "--no-browser"]).count("--no-browser") == 1
