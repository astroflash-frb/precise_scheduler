"""
Schedule publisher for PRECISE Scheduler.

Handles:
  - Committing .key (and .vex) files to the precise_schedules git repo (Issue #10)
  - Compiling the schedule with sched.py (Issue #11)
  - Uploading artefacts to vlbeer via scp (Issue #11)
  - Slack success notification and Mattermost station announcement (Issue #11)
"""

from __future__ import annotations

import json
import logging
import subprocess
import urllib.request
from datetime import datetime
from pathlib import Path

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from .config import Config

log = logging.getLogger(__name__)


class PublisherError(Exception):
    """Raised when any publisher operation fails."""


def slack_success_message(project: str) -> str:
    """Return the Slack notification text posted once *project* is uploaded."""
    return (
        f"Schedule for {project} has been uploaded to vlbeer "
        f"and committed to the schedules repository."
    )


def mattermost_message(project: str) -> str:
    """Return the Mattermost station announcement text for *project*."""
    return f"Schedule for {project} is available on vlbeer. Please download."


class SchedulePublisher:
    def __init__(self, config: Config) -> None:
        self.config = config

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def commit_schedule(
        self,
        project: str,
        draft: bool = True,
        path: Path | None = None,
        *,
        debug: bool = False,
    ) -> None:
        """
        Stage and commit the .key (and .vex if present) files for `project`
        in the precise_schedules git repo.

        Steps:
        1. git pull origin main  (always pull before commit)
        2. git add <project_files>  (glob for <project>*.key and <project>*.vex)
        3. git commit -m "Draft schedule for <PROJECT>"   (if draft=True)
                    or -m "Final schedule for <PROJECT>"  (if draft=False)
        4. git push origin main

        `path`: if given, look for project files there; otherwise use
        self.config.schedules_repo.

        Raises PublisherError on any git failure (non-zero exit code).
        """
        repo_path = path if path is not None else self.config.schedules_repo
        commit_msg = (
            f"Draft schedule for {project}"
            if draft
            else f"Final schedule for {project}"
        )

        if debug:
            print(f"[DEBUG] Would git pull in {repo_path}")
            print(f"[DEBUG] Would git add {project}*.key {project}*.vex")
            print(f"[DEBUG] Would git commit -m '{commit_msg}'")
            print("[DEBUG] Would git push origin main")
            log.info("[DEBUG] Skipping git operations for project %s (debug mode).", project)
            return

        log.info("Pulling latest changes in %s", repo_path)
        self._run(["git", "pull", "origin", "main"], cwd=repo_path)

        # Collect files to stage (only files that actually exist in the repo dir)
        files_to_add: list[Path] = []
        for pattern in (f"{project}*.key", f"{project}*.vex"):
            files_to_add.extend(repo_path.glob(pattern))

        if files_to_add:
            add_cmd = ["git", "add"] + [str(f) for f in files_to_add]
            log.debug("Staging files: %s", [f.name for f in files_to_add])
            self._run(add_cmd, cwd=repo_path)
        else:
            log.warning("No .key or .vex files found for project %s in %s", project, repo_path)

        log.info("Committing: %s", commit_msg)
        self._run(["git", "commit", "-m", commit_msg], cwd=repo_path)

        log.info("Pushing to origin main")
        self._run(["git", "push", "origin", "main"], cwd=repo_path)

    def compile_schedule(
        self,
        key_file: Path,
        output_path: Path | None = None,
        *,
        debug: bool = False,
    ) -> Path:
        """
        Compile the schedule using ``sched.py -k <key_file>`` (found on PATH).

        Runs in *output_path* (or ``key_file.parent`` if None).
        Returns the directory containing the compiled artefacts.
        In debug mode, prints the command that would be run instead of executing it.
        """
        cwd = output_path if output_path is not None else key_file.parent
        cmd = ["sched.py", "-k", str(key_file)]
        if debug:
            print(f"[DEBUG] Would run: {' '.join(cmd)}  (cwd={cwd})")
            log.info("[DEBUG] Skipping sched.py execution (debug mode).")
        else:
            log.info("Compiling schedule: sched.py -k %s (cwd=%s)", key_file, cwd)
            self._run(cmd, cwd=cwd)
        return cwd

    def upload_and_report(
        self,
        repo_path: Path,
        project: str,
        obs_month: datetime | None = None,
        *,
        debug: bool = False,
    ) -> None:
        """
        Full finalisation sequence:
        1. git pull in repo_path
        2. Glob for <project>*.key in repo_path; compile with compile_schedule()
        3. scp all artefacts (<project>*.key, <project>*.sum, <project>*.vex)
           to <antenna_user>@<antenna_host>:<antenna_base_path>/<mmmYY>/
           where mmmYY is derived from obs_month (or the .key file mtime if None)
        4. commit_schedule(project, draft=False, path=repo_path)
        5. post_slack_success(project)  — post to Slack channel
        6. post_mattermost(project)     — post to Mattermost webhook

        Raises PublisherError if any step fails.
        If step 3 (scp) fails: raise immediately, do NOT proceed to step 4–6.
        """
        # Step 1: pull
        if debug:
            print(f"[DEBUG] Would git pull in {repo_path}")
        else:
            log.info("Step 1: pulling latest changes in %s", repo_path)
            self._run(["git", "pull", "origin", "main"], cwd=repo_path)

        # Step 2: compile
        key_files = list(repo_path.glob(f"{project}*.key"))
        if not key_files:
            raise PublisherError(f"No .key file found for project {project} in {repo_path}")
        key_file = key_files[0]
        log.info("Step 2: compiling schedule from %s", key_file)
        artefact_dir = self.compile_schedule(key_file, output_path=repo_path, debug=debug)

        # Determine the remote month subdirectory
        if obs_month is not None:
            month_str = self._mmmyy(obs_month)
        else:
            mtime = key_file.stat().st_mtime
            month_str = self._mmmyy(datetime.fromtimestamp(mtime))
        remote_path = f"{self.config.antenna_base_path}/{month_str}"

        # Gather artefacts to upload
        artefacts: list[Path] = []
        for pattern in (f"{project}*.key", f"{project}*.sum", f"{project}*.vex"):
            artefacts.extend(artefact_dir.glob(pattern))

        # Step 3: scp — if this fails, stop immediately (do NOT proceed to 4–6)
        if debug:
            print(f"[DEBUG] Would scp {len(artefacts)} artefact(s) to {remote_path}/")
            for f in artefacts:
                print(f"[DEBUG]   {f.name}")
            log.info("[DEBUG] Skipping scp upload (debug mode).")
        else:
            log.info("Step 3: uploading %d artefact(s) to %s", len(artefacts), remote_path)
            self._scp_files(artefacts, remote_path)

        # Step 4: final commit
        log.info("Step 4: committing final schedule for %s", project)
        self.commit_schedule(project, draft=False, path=repo_path, debug=debug)

        # Step 5: Slack notification
        log.info("Step 5: posting Slack success notification")
        self.post_slack_success(project, debug=debug)

        # Step 6: Mattermost notification
        log.info("Step 6: posting Mattermost station announcement")
        self.post_mattermost(project, debug=debug)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run(
        self,
        cmd: list[str],
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess:
        """Run a subprocess command. Raise PublisherError on non-zero exit."""
        log.debug("Running: %s (cwd=%s)", cmd, cwd)
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            cwd=str(cwd) if cwd is not None else None,
        )
        if result.returncode != 0:
            raise PublisherError(
                f"Command {cmd!r} failed (exit {result.returncode}).\n"
                f"stdout: {result.stdout}\n"
                f"stderr: {result.stderr}"
            )
        return result

    def _scp_files(self, files: list[Path], remote_path: str) -> None:
        """scp files to remote_path using the configured SSH key.

        Command:
            scp -i <ssh_key> -o StrictHostKeyChecking=no <files...>
                <user>@<host>:<remote_path>/
        """
        if not files:
            log.warning("_scp_files called with an empty file list — nothing to upload.")
            return

        destination = (
            f"{self.config.antenna_user}@{self.config.antenna_host}:{remote_path}/"
        )
        cmd = [
            "scp",
            "-i", str(self.config.antenna_ssh_key),
            "-o", "StrictHostKeyChecking=no",
            *[str(f) for f in files],
            destination,
        ]
        self._run(cmd)

    def _mmmyy(self, dt: datetime) -> str:
        """Format datetime as mmmYY, e.g. datetime(2026, 7, 15) → 'Jul26'."""
        return dt.strftime("%b%y")

    def post_slack_success(
        self,
        project: str,
        client: WebClient | None = None,
        *,
        debug: bool = False,
    ) -> None:
        """Post upload-success message to Slack.

        `client` is a slack_sdk.WebClient (injected for testing; built from
        config when None).
        """
        message = slack_success_message(project)

        if debug:
            print(f"[DEBUG] Would post to Slack channel "
                  f"{self.config.slack_channel_id}: {message}")
            log.info("[DEBUG] Skipping Slack notification (debug mode).")
            return

        if client is None:
            client = WebClient(token=self.config.slack_bot_token)

        try:
            client.chat_postMessage(channel=self.config.slack_channel_id, text=message)
            log.info("Slack success notification sent for project %s", project)
        except SlackApiError as exc:
            log.error(
                "Failed to post Slack success notification for %s: %s",
                project,
                exc.response["error"],
            )
        except Exception as exc:  # noqa: BLE001
            log.error(
                "Unexpected error posting Slack notification for %s: %s",
                project,
                exc,
            )

    def post_mattermost(self, project: str, *, debug: bool = False) -> None:
        """POST antenna notification to the Mattermost webhook URL.

        Uses urllib.request (stdlib) — no extra dependency.
        Payload: {"text": "Schedule for <PROJECT> is available on vlbeer. Please download."}
        """
        message = mattermost_message(project)
        if debug:
            print(f"[DEBUG] Would POST to Mattermost webhook: {message}")
            log.info("[DEBUG] Skipping Mattermost notification (debug mode).")
            return

        payload = {"text": message}
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url=self.config.mattermost_webhook_url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req) as response:
                log.info(
                    "Mattermost notification sent for project %s (status=%s)",
                    project,
                    response.status,
                )
        except Exception as exc:  # noqa: BLE001
            log.error(
                "Failed to post Mattermost notification for %s: %s",
                project,
                exc,
            )


# --------------------------------------------------------------------------- #
# Read-only checks used by `precise calendar`                                  #
# --------------------------------------------------------------------------- #

#: How long to wait on vlbeer before giving up on the check.  `precise
#: calendar` is meant to answer quickly; an unreachable server should slow it
#: down by seconds, not block it.
VLBEER_TIMEOUT_SECONDS = 15


def vlbeer_uploaded(
    config: Config, months: set[str], *, timeout: float = VLBEER_TIMEOUT_SECONDS
) -> set[str] | None:
    """Project codes that already have a ``.key`` or ``.vex`` on vlbeer.

    One ssh call lists every month directory the caller cares about, rather
    than one call per observation.  Names come back as they are on disk, so
    the caller matches case-insensitively.

    Returns ``None`` when vlbeer cannot be reached at all — which is not the
    same answer as "nothing is uploaded", and the caller should say so.
    """
    if not months:
        return set()

    base = config.antenna_base_path
    listing = " ".join(f"{base}/{month}" for month in sorted(months))
    cmd = [
        "ssh",
        "-i", str(config.antenna_ssh_key),
        "-o", "StrictHostKeyChecking=no",
        "-o", f"ConnectTimeout={int(timeout)}",
        "-o", "BatchMode=yes",
        f"{config.antenna_user}@{config.antenna_host}",
        # Missing month directories are normal, so failures are swallowed here
        # and an empty listing simply means nothing is uploaded yet.
        f"ls {listing} 2>/dev/null || true",
    ]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=timeout
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Could not reach vlbeer to check uploads: %s", exc)
        return None

    if result.returncode != 0:
        log.warning(
            "Could not list vlbeer (exit %d): %s",
            result.returncode, result.stderr.strip()[:200],
        )
        return None

    uploaded: set[str] = set()
    for line in result.stdout.splitlines():
        name = line.strip()
        stem, _, suffix = name.rpartition(".")
        if stem and suffix.lower() in ("key", "vex"):
            uploaded.add(stem.upper())
    return uploaded


def local_key_files(repo_path: Path) -> set[str]:
    """Project codes that already have a ``.key`` in the schedules repository."""
    return {path.stem.upper() for path in Path(repo_path).glob("*.key")}


def month_dir(dt: datetime) -> str:
    """vlbeer's per-month directory name, e.g. ``Sep26``."""
    return dt.strftime("%b%y")
