"""Tests for precise_scheduler/publisher.py.

All subprocess/network calls are mocked. git commands use mocker.patch.object
on _run; scp and notification helpers are patched independently.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from precise_scheduler.publisher import (
    local_key_files,
    month_dir,
    vlbeer_uploaded,
)
from precise_scheduler.publisher import PublisherError, SchedulePublisher


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_config(tmp_path: Path) -> SimpleNamespace:
    """Return a Config-like object with tmp_path as schedules_repo."""
    return SimpleNamespace(
        schedules_repo=tmp_path,
        sched_bin=Path("/usr/local/bin/sched.py"),
        antenna_host="vlbeer.ira.inaf.it",
        antenna_user="vlbeer_user",
        antenna_ssh_key=Path("/home/user/.ssh/id_rsa"),
        antenna_base_path="vlbi_arch",
        slack_bot_token="xoxb-test-token",
        slack_channel_id="C0123456789",
        mattermost_webhook_url="https://mattermost.example.com/hooks/abcdef",
    )


def make_git_repo(path: Path) -> None:
    """Init a real bare-minimum git repo in path so git commands work."""
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@test.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )


def _ok_result(**kwargs: Any) -> subprocess.CompletedProcess:
    """Return a CompletedProcess with returncode=0."""
    defaults = dict(args=[], returncode=0, stdout="", stderr="")
    defaults.update(kwargs)
    return subprocess.CompletedProcess(**defaults)


# ---------------------------------------------------------------------------
# commit_schedule tests
# ---------------------------------------------------------------------------


class TestCommitSchedule:
    """commit_schedule stages key/vex files and runs git pull/add/commit/push."""

    def _make_publisher(self, tmp_path: Path) -> SchedulePublisher:
        return SchedulePublisher(make_config(tmp_path))

    def test_commit_schedule_draft_message(
        self, tmp_path: Path, mocker: "pytest_mock.MockerFixture"
    ) -> None:
        """_run is called with a commit message containing 'Draft schedule for RP036A'."""
        publisher = self._make_publisher(tmp_path)
        run_mock = mocker.patch.object(publisher, "_run", return_value=_ok_result())

        publisher.commit_schedule("RP036A", draft=True, path=tmp_path)

        commit_calls = [
            call
            for call in run_mock.call_args_list
            if call.args and call.args[0][0] == "git" and "commit" in call.args[0]
        ]
        assert commit_calls, "Expected at least one 'git commit' call"
        commit_cmd = commit_calls[0].args[0]
        msg_idx = commit_cmd.index("-m") + 1
        assert "Draft schedule for RP036A" in commit_cmd[msg_idx]

    def test_commit_schedule_final_message(
        self, tmp_path: Path, mocker: "pytest_mock.MockerFixture"
    ) -> None:
        """_run is called with a commit message containing 'Final schedule for RP036A'."""
        publisher = self._make_publisher(tmp_path)
        run_mock = mocker.patch.object(publisher, "_run", return_value=_ok_result())

        publisher.commit_schedule("RP036A", draft=False, path=tmp_path)

        commit_calls = [
            call
            for call in run_mock.call_args_list
            if call.args and call.args[0][0] == "git" and "commit" in call.args[0]
        ]
        assert commit_calls, "Expected at least one 'git commit' call"
        commit_cmd = commit_calls[0].args[0]
        msg_idx = commit_cmd.index("-m") + 1
        assert "Final schedule for RP036A" in commit_cmd[msg_idx]

    def test_commit_schedule_pull_before_push(
        self, tmp_path: Path, mocker: "pytest_mock.MockerFixture"
    ) -> None:
        """git pull is called before git push."""
        publisher = self._make_publisher(tmp_path)
        call_order: list[str] = []

        def record_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
            if "pull" in cmd:
                call_order.append("pull")
            elif "push" in cmd:
                call_order.append("push")
            return _ok_result(args=cmd)

        mocker.patch.object(publisher, "_run", side_effect=record_run)

        publisher.commit_schedule("RP036A", path=tmp_path)

        assert "pull" in call_order, "git pull not called"
        assert "push" in call_order, "git push not called"
        assert call_order.index("pull") < call_order.index("push"), (
            "git pull must be called before git push"
        )

    def test_commit_schedule_raises_on_git_failure(
        self, tmp_path: Path, mocker: "pytest_mock.MockerFixture"
    ) -> None:
        """PublisherError from _run propagates out of commit_schedule."""
        publisher = self._make_publisher(tmp_path)
        mocker.patch.object(
            publisher,
            "_run",
            side_effect=PublisherError("git pull failed"),
        )

        with pytest.raises(PublisherError, match="git pull failed"):
            publisher.commit_schedule("RP036A", path=tmp_path)


# ---------------------------------------------------------------------------
# compile_schedule tests
# ---------------------------------------------------------------------------


class TestCompileSchedule:
    """compile_schedule invokes sched.py and returns the output directory."""

    def _make_publisher(self, tmp_path: Path) -> SchedulePublisher:
        return SchedulePublisher(make_config(tmp_path))

    def test_compile_schedule_calls_sched_py(
        self, tmp_path: Path, mocker: "pytest_mock.MockerFixture"
    ) -> None:
        """_run is called with sched.py -- taken from PATH -- and the key file."""
        publisher = self._make_publisher(tmp_path)
        run_mock = mocker.patch.object(publisher, "_run", return_value=_ok_result())

        key_file = tmp_path / "RP036A.key"
        key_file.touch()

        publisher.compile_schedule(key_file)

        assert run_mock.call_args.args[0] == ["sched.py", "-k", str(key_file)]
        assert run_mock.call_args.kwargs["cwd"] == key_file.parent

    def test_compile_schedule_returns_directory(
        self, tmp_path: Path, mocker: "pytest_mock.MockerFixture"
    ) -> None:
        """compile_schedule returns a Path (the output directory)."""
        publisher = self._make_publisher(tmp_path)
        mocker.patch.object(publisher, "_run", return_value=_ok_result())

        key_file = tmp_path / "RP036A.key"
        key_file.touch()

        result = publisher.compile_schedule(key_file)

        assert isinstance(result, Path)

    def test_compile_schedule_uses_key_parent_when_no_output_path(
        self, tmp_path: Path, mocker: "pytest_mock.MockerFixture"
    ) -> None:
        """When output_path is None, the returned directory equals key_file.parent."""
        publisher = self._make_publisher(tmp_path)
        mocker.patch.object(publisher, "_run", return_value=_ok_result())

        key_file = tmp_path / "RP036A.key"
        key_file.touch()

        result = publisher.compile_schedule(key_file)

        assert result == tmp_path

    def test_compile_schedule_uses_explicit_output_path(
        self, tmp_path: Path, mocker: "pytest_mock.MockerFixture"
    ) -> None:
        """When output_path is given, it is used as cwd and returned."""
        publisher = self._make_publisher(tmp_path)
        run_mock = mocker.patch.object(publisher, "_run", return_value=_ok_result())

        key_file = tmp_path / "RP036A.key"
        key_file.touch()
        out_dir = tmp_path / "compiled"
        out_dir.mkdir()

        result = publisher.compile_schedule(key_file, output_path=out_dir)

        assert result == out_dir
        assert run_mock.call_args.kwargs.get("cwd") == out_dir

    def test_compile_schedule_raises_on_sched_failure(
        self, tmp_path: Path, mocker: "pytest_mock.MockerFixture"
    ) -> None:
        """PublisherError from _run propagates out of compile_schedule."""
        publisher = self._make_publisher(tmp_path)
        mocker.patch.object(
            publisher,
            "_run",
            side_effect=PublisherError("sched.py exited with code 1"),
        )

        key_file = tmp_path / "RP036A.key"
        key_file.touch()

        with pytest.raises(PublisherError, match="sched.py exited with code 1"):
            publisher.compile_schedule(key_file)


# ---------------------------------------------------------------------------
# upload_and_report tests
# ---------------------------------------------------------------------------


class TestUploadAndReport:
    """upload_and_report orchestrates compile → scp → commit → notify."""

    PROJECT = "RP036A"

    def _make_publisher_with_key(self, tmp_path: Path) -> SchedulePublisher:
        """Create a publisher and place a dummy .key file in tmp_path."""
        publisher = SchedulePublisher(make_config(tmp_path))
        (tmp_path / f"{self.PROJECT}.key").touch()
        return publisher

    def test_upload_and_report_full_sequence(
        self, tmp_path: Path, mocker: "pytest_mock.MockerFixture"
    ) -> None:
        """All steps (pull, compile, scp, commit, slack, mattermost) are called."""
        publisher = self._make_publisher_with_key(tmp_path)

        run_mock = mocker.patch.object(publisher, "_run", return_value=_ok_result())
        scp_mock = mocker.patch.object(publisher, "_scp_files")
        slack_mock = mocker.patch.object(publisher, "post_slack_success")
        mm_mock = mocker.patch.object(publisher, "post_mattermost")

        obs_month = datetime(2026, 7, 15, tzinfo=timezone.utc)
        publisher.upload_and_report(tmp_path, self.PROJECT, obs_month=obs_month)

        # git pull was called
        pull_calls = [
            c for c in run_mock.call_args_list if "pull" in c.args[0]
        ]
        assert pull_calls, "git pull not called"

        # scp was called
        scp_mock.assert_called_once()

        # commit was called (via commit_schedule which calls _run with 'commit')
        commit_calls = [
            c for c in run_mock.call_args_list if "commit" in c.args[0]
        ]
        assert commit_calls, "git commit not called"

        # notifications sent, both carrying the debug flag through
        slack_mock.assert_called_once_with(self.PROJECT, debug=False)
        mm_mock.assert_called_once_with(self.PROJECT, debug=False)

    def test_upload_and_report_scp_failure_stops_sequence(
        self, tmp_path: Path, mocker: "pytest_mock.MockerFixture"
    ) -> None:
        """If scp raises PublisherError, commit and notifications must NOT be called."""
        publisher = self._make_publisher_with_key(tmp_path)

        mocker.patch.object(publisher, "_run", return_value=_ok_result())
        mocker.patch.object(
            publisher,
            "_scp_files",
            side_effect=PublisherError("scp failed"),
        )
        commit_mock = mocker.patch.object(publisher, "commit_schedule")
        slack_mock = mocker.patch.object(publisher, "post_slack_success")
        mm_mock = mocker.patch.object(publisher, "post_mattermost")

        with pytest.raises(PublisherError, match="scp failed"):
            publisher.upload_and_report(tmp_path, self.PROJECT)

        commit_mock.assert_not_called()
        slack_mock.assert_not_called()
        mm_mock.assert_not_called()

    def test_upload_and_report_final_commit_message(
        self, tmp_path: Path, mocker: "pytest_mock.MockerFixture"
    ) -> None:
        """commit_schedule is called with draft=False for the project."""
        publisher = self._make_publisher_with_key(tmp_path)

        mocker.patch.object(publisher, "_run", return_value=_ok_result())
        mocker.patch.object(publisher, "_scp_files")
        mocker.patch.object(publisher, "post_slack_success")
        mocker.patch.object(publisher, "post_mattermost")
        commit_mock = mocker.patch.object(publisher, "commit_schedule")

        publisher.upload_and_report(tmp_path, self.PROJECT)

        commit_mock.assert_called_once()
        # upload_and_report calls: commit_schedule(project, draft=False, path=repo_path)
        # so args[0] is project, and draft is always passed as a keyword argument.
        call = commit_mock.call_args
        assert call.args[0] == self.PROJECT
        assert call.kwargs.get("draft") is False

    def test_upload_and_report_no_key_file_raises(
        self, tmp_path: Path, mocker: "pytest_mock.MockerFixture"
    ) -> None:
        """PublisherError is raised when no .key file matches the project."""
        publisher = SchedulePublisher(make_config(tmp_path))
        mocker.patch.object(publisher, "_run", return_value=_ok_result())

        with pytest.raises(PublisherError, match="No .key file found"):
            publisher.upload_and_report(tmp_path, "RP999Z")

    def test_upload_and_report_uses_mmmyy_from_obs_month(
        self, tmp_path: Path, mocker: "pytest_mock.MockerFixture"
    ) -> None:
        """The remote path passed to _scp_files contains the mmmYY from obs_month."""
        publisher = self._make_publisher_with_key(tmp_path)

        mocker.patch.object(publisher, "_run", return_value=_ok_result())
        scp_mock = mocker.patch.object(publisher, "_scp_files")
        mocker.patch.object(publisher, "post_slack_success")
        mocker.patch.object(publisher, "post_mattermost")

        obs_month = datetime(2026, 7, 1, tzinfo=timezone.utc)
        publisher.upload_and_report(tmp_path, self.PROJECT, obs_month=obs_month)

        assert scp_mock.called
        remote_path_arg = scp_mock.call_args.args[1]
        assert "Jul26" in remote_path_arg


# ---------------------------------------------------------------------------
# _mmmyy tests  (pure function, no mocking)
# ---------------------------------------------------------------------------


class TestMmmyy:
    """_mmmyy formats a datetime as mmmYY."""

    def _pub(self, tmp_path: Path) -> SchedulePublisher:
        return SchedulePublisher(make_config(tmp_path))

    def test_mmmyy_july_2026(self, tmp_path: Path) -> None:
        pub = self._pub(tmp_path)
        assert pub._mmmyy(datetime(2026, 7, 15)) == "Jul26"

    def test_mmmyy_january_2027(self, tmp_path: Path) -> None:
        pub = self._pub(tmp_path)
        assert pub._mmmyy(datetime(2027, 1, 1)) == "Jan27"

    def test_mmmyy_december_2025(self, tmp_path: Path) -> None:
        pub = self._pub(tmp_path)
        assert pub._mmmyy(datetime(2025, 12, 31)) == "Dec25"


# ---------------------------------------------------------------------------
# post_mattermost tests
# ---------------------------------------------------------------------------


class TestPostMattermost:
    """post_mattermost sends a JSON payload containing the project name."""

    def _pub(self, tmp_path: Path) -> SchedulePublisher:
        return SchedulePublisher(make_config(tmp_path))

    def test_post_mattermost_sends_project_name(
        self, tmp_path: Path, mocker: "pytest_mock.MockerFixture"
    ) -> None:
        """The HTTP request payload contains the project name."""
        pub = self._pub(tmp_path)

        captured: list[Any] = []

        class _FakeResponse:
            status = 200

            def __enter__(self) -> "_FakeResponse":
                return self

            def __exit__(self, *args: Any) -> None:
                pass

        def fake_urlopen(req: Any) -> _FakeResponse:
            captured.append(req)
            return _FakeResponse()

        mocker.patch("precise_scheduler.publisher.urllib.request.urlopen", side_effect=fake_urlopen)

        pub.post_mattermost("RP036A")

        assert captured, "urlopen was not called"
        req = captured[0]
        # req.data is bytes
        payload = json.loads(req.data.decode("utf-8"))
        assert "RP036A" in payload["text"]

    def test_post_mattermost_uses_webhook_url(
        self, tmp_path: Path, mocker: "pytest_mock.MockerFixture"
    ) -> None:
        """The request is sent to the configured webhook URL."""
        pub = self._pub(tmp_path)
        captured: list[Any] = []

        class _FakeResponse:
            status = 200

            def __enter__(self) -> "_FakeResponse":
                return self

            def __exit__(self, *args: Any) -> None:
                pass

        def fake_urlopen(req: Any) -> _FakeResponse:
            captured.append(req)
            return _FakeResponse()

        mocker.patch("precise_scheduler.publisher.urllib.request.urlopen", side_effect=fake_urlopen)

        pub.post_mattermost("RP036A")

        assert captured[0].full_url == pub.config.mattermost_webhook_url

    def test_post_mattermost_does_not_raise_on_error(
        self, tmp_path: Path, mocker: "pytest_mock.MockerFixture"
    ) -> None:
        """Network errors are logged, not raised."""
        pub = self._pub(tmp_path)

        mocker.patch(
            "precise_scheduler.publisher.urllib.request.urlopen",
            side_effect=OSError("connection refused"),
        )

        # Should not raise
        pub.post_mattermost("RP036A")


# ---------------------------------------------------------------------------
# post_slack_success tests
# ---------------------------------------------------------------------------


class TestPostSlackSuccess:
    """post_slack_success posts to the configured Slack channel."""

    def _pub(self, tmp_path: Path) -> SchedulePublisher:
        return SchedulePublisher(make_config(tmp_path))

    def test_post_slack_success_calls_chat_post_message(
        self, tmp_path: Path
    ) -> None:
        """Injected Slack client receives chat_postMessage with project name in text."""
        pub = self._pub(tmp_path)
        mock_client = MagicMock()

        pub.post_slack_success("RP036A", client=mock_client)

        mock_client.chat_postMessage.assert_called_once()
        call_kwargs = mock_client.chat_postMessage.call_args.kwargs
        assert "RP036A" in call_kwargs.get("text", "")
        assert call_kwargs.get("channel") == pub.config.slack_channel_id

    def test_post_slack_success_does_not_raise_on_error(
        self, tmp_path: Path
    ) -> None:
        """SlackApiError is caught and logged, not re-raised."""
        from slack_sdk.errors import SlackApiError

        pub = self._pub(tmp_path)
        mock_client = MagicMock()
        mock_client.chat_postMessage.side_effect = SlackApiError(
            message="channel_not_found",
            response={"error": "channel_not_found"},
        )

        # Should not raise
        pub.post_slack_success("RP036A", client=mock_client)

    def test_post_slack_success_debug_does_not_post(self, tmp_path: Path) -> None:
        """Debug mode must send nothing — it is the whole point of the flag."""
        pub = self._pub(tmp_path)
        mock_client = MagicMock()

        pub.post_slack_success("RP036A", client=mock_client, debug=True)

        mock_client.chat_postMessage.assert_not_called()

    def test_post_slack_success_debug_builds_no_client(
        self, tmp_path: Path, mocker
    ) -> None:
        """Debug mode must not even construct a WebClient from the real token."""
        web_client = mocker.patch("precise_scheduler.publisher.WebClient")

        self._pub(tmp_path).post_slack_success("RP036A", debug=True)

        web_client.assert_not_called()

    def test_post_slack_success_debug_prints_message(
        self, tmp_path: Path, capsys
    ) -> None:
        """The message that would have been posted is shown instead."""
        self._pub(tmp_path).post_slack_success("RP036A", debug=True)

        out = capsys.readouterr().out
        assert "RP036A" in out
        assert "DEBUG" in out


# ---------------------------------------------------------------------------
# _run tests
# ---------------------------------------------------------------------------


class TestRun:
    """_run raises PublisherError on non-zero exit, returns result on success."""

    def _pub(self, tmp_path: Path) -> SchedulePublisher:
        return SchedulePublisher(make_config(tmp_path))

    def test_run_raises_on_nonzero_exit(self, tmp_path: Path) -> None:
        pub = self._pub(tmp_path)
        with pytest.raises(PublisherError, match="failed"):
            pub._run(["false"])   # 'false' always exits 1

    def test_run_returns_completed_process_on_success(self, tmp_path: Path) -> None:
        pub = self._pub(tmp_path)
        result = pub._run(["true"])
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# Read-only checks used by `precise calendar`
# ---------------------------------------------------------------------------


class TestLocalKeyFiles:
    def test_finds_key_files(self, tmp_path: Path) -> None:
        (tmp_path / "PR411A.key").touch()
        (tmp_path / "PR412A.key").touch()

        assert local_key_files(tmp_path) == {"PR411A", "PR412A"}

    def test_ignores_other_files(self, tmp_path: Path) -> None:
        (tmp_path / "PR411A.key").touch()
        (tmp_path / "PR411A.vex").touch()
        (tmp_path / "targets.toml").touch()

        assert local_key_files(tmp_path) == {"PR411A"}

    def test_names_are_upper_cased_for_matching(self, tmp_path: Path) -> None:
        (tmp_path / "pr411a.key").touch()

        assert local_key_files(tmp_path) == {"PR411A"}

    def test_empty_directory(self, tmp_path: Path) -> None:
        assert local_key_files(tmp_path) == set()


class TestMonthDir:
    def test_formats_as_vlbeer_expects(self) -> None:
        assert month_dir(datetime(2026, 9, 7, tzinfo=timezone.utc)) == "Sep26"
        assert month_dir(datetime(2026, 12, 31, tzinfo=timezone.utc)) == "Dec26"


class TestVlbeerUploaded:
    def _config(self, tmp_path: Path):
        return SimpleNamespace(
            antenna_host="vlbeer.example",
            antenna_user="vlbeer",
            antenna_ssh_key=tmp_path / "key",
            antenna_base_path="vlbi_arch",
        )

    def test_returns_projects_with_key_or_vex(
        self, tmp_path: Path, mocker
    ) -> None:
        run = mocker.patch("precise_scheduler.publisher.subprocess.run")
        run.return_value = SimpleNamespace(
            returncode=0,
            stdout="pr411a.key\npr405a.vex\nnotes.txt\n",
            stderr="",
        )

        assert vlbeer_uploaded(self._config(tmp_path), {"Sep26"}) == {
            "PR411A", "PR405A"
        }

    def test_lists_every_month_in_one_call(self, tmp_path: Path, mocker) -> None:
        """One ssh round trip, not one per observation."""
        run = mocker.patch("precise_scheduler.publisher.subprocess.run")
        run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")

        vlbeer_uploaded(self._config(tmp_path), {"Sep26", "Oct26"})

        assert run.call_count == 1
        command = run.call_args.args[0][-1]
        assert "vlbi_arch/Sep26" in command
        assert "vlbi_arch/Oct26" in command

    def test_no_months_makes_no_call(self, tmp_path: Path, mocker) -> None:
        run = mocker.patch("precise_scheduler.publisher.subprocess.run")

        assert vlbeer_uploaded(self._config(tmp_path), set()) == set()
        run.assert_not_called()

    def test_unreachable_server_is_unknown_not_empty(
        self, tmp_path: Path, mocker
    ) -> None:
        """"Cannot tell" and "nothing uploaded" are different answers."""
        run = mocker.patch("precise_scheduler.publisher.subprocess.run")
        run.return_value = SimpleNamespace(
            returncode=255, stdout="", stderr="Permission denied"
        )

        assert vlbeer_uploaded(self._config(tmp_path), {"Sep26"}) is None

    def test_timeout_is_unknown(self, tmp_path: Path, mocker) -> None:
        mocker.patch(
            "precise_scheduler.publisher.subprocess.run",
            side_effect=subprocess.TimeoutExpired("ssh", 15),
        )

        assert vlbeer_uploaded(self._config(tmp_path), {"Sep26"}) is None

    def test_missing_ssh_binary_is_unknown(self, tmp_path: Path, mocker) -> None:
        mocker.patch(
            "precise_scheduler.publisher.subprocess.run",
            side_effect=OSError("no ssh"),
        )

        assert vlbeer_uploaded(self._config(tmp_path), {"Sep26"}) is None

    def test_empty_listing_means_nothing_uploaded(
        self, tmp_path: Path, mocker
    ) -> None:
        run = mocker.patch("precise_scheduler.publisher.subprocess.run")
        run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")

        assert vlbeer_uploaded(self._config(tmp_path), {"Sep26"}) == set()
