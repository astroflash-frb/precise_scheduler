# PRD: PRECISE Observation Scheduler

## Problem Statement

PRECISE (Pinpointing Repeating and Classifying Enigmatic Sources in Extragalactic Epochs) runs VLBI observing campaigns every one to two weeks, targeting known repeating FRB sources. Every step of the scheduling process is currently manual: a team member reads the Google Sheets calendar to find the next allocated time, cross-checks recent CHIME VOEvents to identify active sources, consults the target list to pick scientifically interesting sources, runs `planobs` to verify visibility, writes the `.key` schedule file by hand, circulates it for team review over Slack, and finally uploads the compiled file to the antenna server via SSH.

This workflow has several failure modes: active FRBs can be missed if no one notices a VOEvent before the scheduling deadline; there is no systematic history of FRB activity to inform long-term prioritisation; the deadline pressure (5–10 days before observations) means scheduling is often rushed; and the entire chain depends on a single person's availability and memory.

The gap: there is no automated system that monitors the observation calendar, tracks source activity, produces an evidence-based schedule proposal, solicits structured team input, and handles the mechanical steps of file generation and distribution.

---

## Solution

A Python daemon (`precise-scheduler`) runs persistently on a group server. It monitors the PRECISE Google Sheets calendar and, ten days before each scheduled observation, automatically:

1. Retrieves the observation window and participating antennas from Google Sheets.
2. Scores every target in `targets.toml` using its priority label and recent detections in the activity database.
3. Calls `planobs` to compute per-antenna, per-15-minute-slot visibility for each candidate source.
4. Posts a structured Slack message containing: FRBs active in the last 15 days; FRBs active in the last 30 days; with how many bursts were reported; a colour-coded visibility grid (green = all antennas can observe, amber = all but one, blank = not observable); and a proposed source list of 1–4 sources.
5. Waits for a team member to confirm or modify the proposal using Slack Block Kit interactive buttons.
6. On confirmation, runs `planobs` to generate the `.key` schedule file and commits it to the repository with the message `"Draft schedule for <PROJECT>"`.
7. When a team member later issues a finalise command (via Slack or CLI), the daemon uploads the compiled `.key` to the antenna server via `scp` and pushes a final commit. It announces it in the appropriate Mattermost channel.

The activity database is a plain file (CSV or TOML) committed to the `precise_schedules` repository. It is populated both from historical records entered manually and automatically whenever the existing VOEvent handler processes a new CHIME alert.

---

## User Stories

### Activity tracking

1. As a PRECISE team member, I want every incoming CHIME VOEvent to be recorded in the activity database automatically, so that source activity history is always current without manual effort.
2. As a PRECISE team member, I want to manually add historical detections (pre-system) to the activity database, so that the full known history of each source is available to the scheduler.
3. As a PRECISE team member, I want to correct or delete an erroneous activity record, so that bad data does not bias future scheduling decisions.
4. As a PRECISE team member, I want to query the activity database for a given source and time window, so that I can audit what the scheduler used to make its proposal.
5. As a PRECISE team member, I want non-CHIME detections (e.g. from DSA or FAST) to be recorded with the correct facility label, so that multi-facility activity is visible.

### Target list management

6. As a PRECISE team member, I want to assign a priority label (0, 1, or 2) to each target in `targets.toml`, so that the scheduler can distinguish sources that should always be observed from those that only warrant time when active.
7. As a PRECISE team member, I want each target to carry a `canonical_name` field (e.g. `FRB20200223D`) that maps to the CHIME/TNS identifier used in VOEvents, so that activity records can be linked to targets unambiguously.
8. As a PRECISE team member, I want to add a new target to `targets.toml` without touching any other configuration file, and have the scheduler consider it in the next run.

### Pre-observation Slack proposal

9. As a PRECISE team member, I want to receive a Slack message automatically ten days before each observation, so that I do not need to remember to check the calendar.
10. As a PRECISE team member, I want the Slack message to show which FRBs were detected in the last 15 days and in the last 30 days, so that I can quickly assess recent activity across all sources.
11. As a PRECISE team member, I want the Slack message to include a visibility grid showing which candidate sources are observable by all antennas (green), all-but-one antennas (amber), or not observable (blank) in 15-minute slots across the observation window, so that I can judge scheduling feasibility at a glance.
12. As a PRECISE team member, I want the Slack message to show the proposed source list with the reasoning (priority level, days since last detection, visibility fraction), so that I understand why each source was selected or excluded.
13. As a PRECISE team member, I want the Slack message to show the FRB sources that have been observed in the last PRECISE runs (last 5 runs or last 3 months, whatever is shorter), and how much time was spent on them, to be retrieved from the `.vex` file past observations.
14. As a PRECISE team member, I want to confirm the proposed source list with a single Slack button click, so that I do not need to type anything when the proposal is already correct.
15. As a PRECISE team member, I want to override the proposed source list by selecting different sources from a Slack dropdown, so that I can make changes without leaving Slack.
16. As a PRECISE team member, I want to receive a reminder in the same Slack thread if nobody has responded 5 days before the observations, so that the proposal does not go unnoticed.
17. As a PRECISE team member, I want the scheduler to wait indefinitely for a human response (after one reminder) rather than auto-confirming, so that a human is always in the loop before a schedule is generated.
18. As a PRECISE shift lead, I want only authorised team members to be able to confirm or modify the schedule via Slack, so that random Slack users cannot alter the proposal.

### Schedule generation and git workflow

18. As a PRECISE team member, I want the schedule file (`.key`) to be generated by `planobs` automatically once the source list is confirmed, so that I do not need to run `planobs` manually.
19. As a PRECISE team member, I want the draft schedule committed to the repository automatically with the message `"Draft schedule for <PROJECT>"`, so that it is version-controlled from the moment it is created.
20. As a PRECISE team member, I want to review the committed `.key` file in the repository before it goes to the antennas, so that I retain a final human check.

### Finalisation and upload

21. As a PRECISE team member, I want to trigger the final upload to the antenna server via a single Slack command or CLI invocation, so that publication is explicit and intentional.
22. As a PRECISE team member, I want a final git commit to be pushed automatically when the schedule is uploaded, so that the repository reflects the exact file sent to the antennas.
23. As a PRECISE team member, I want the uploaded file (using the one in the repository, so an automatic pull would need to be done before hand) to be the compiled using `sched.py -k *.key` , and all output files to be copied via `scp` to the `vlbeer.ira.inaf.it` server under the directory `vlbi_arch/mmmYY/` , with `mmmYY` the month and year, so that all antennas can download it in the standard way.
24. As a PRECISE team member, I want to receive a Slack confirmation message when the upload succeeds, so that I know the antennas have the correct file without having to SSH manually.
25. As a PRECISE team member, I want to automatically notify the antennas via a message to be written in the dedicated Mattermost channel when the upload succeeds, so everyone knows that the files are there to be downloaded.

### Error and failure handling

25. As a PRECISE team member, I want to receive a Slack alert if the daemon fails to read the Google Sheets calendar, so that I can intervene before the ten-day window expires.
26. As a PRECISE team member, I want to receive a Slack alert if `planobs` fails or returns no visible sources for any candidate, so that I can debug the issue before scheduling.
27. As a PRECISE team member, I want to receive a Slack alert if the rsync/SCP upload fails, so that I can retry or fall back to manual upload in time for the observation.
28. As a PRECISE team member, I want to receive a Slack alert if no team member has confirmed the schedule with fewer than five days remaining before the observation, so that the deadline is not missed silently.
29. As a PRECISE team member, I want failed runs to be logged to a persistent file on the server, so that I can diagnose issues after the fact.

---

## Implementation Decisions

### targets.toml schema extension

The existing `targets.toml` (in the PRECISE observations repository, separate from `precise_scheduler`) will gain two new fields per entry:

- `priority`: integer 0–2 (0 = no new observations needed; 1 = observe only if recent activity reported; 2 = high scientific priority, observe regardless of activity).
- `canonical_name`: string matching the CHIME/TNS identifier used in VOEvents (e.g. `"FRB20200223D"`). Used as the join key between `targets.toml` and the activity database.

All other existing fields (`name`, `coordinates`, `phasecal`, `checksource`) are preserved unchanged and remain the authoritative input to `planobs`.

### Activity database format

A single CSV file committed to the `precise_scheduler` repository. Each row/record contains:

- `canonical_name`: string, the CHIME/TNS source identifier.
- `utc_datetime`: ISO 8601 datetime string.
- `facility`: string, defaults to `"CHIME"`.

The file is append-only in normal operation; edits/deletions are manual. The file is small enough for human inspection and git diff. No database server is required.

### Google Sheets integration

The daemon reads a designated public sheet that contains the upcoming observations:

* Located at the following URL: https://docs.google.com/spreadsheets/d/14Pntaz1FvsYyAOzJYjqHk3UpLzJ2gsbBzWrIqMffxYo/edit?usp=sharing.
* Each tab is for a particular month of the year (formatted as mmmYY). If the month is not available, means that no observations will be performed. Then there are different rows with the different observations (in general with an empty row in between). The first column contains the project name, the second one the date (``D1-D2 Mmm`, where it means to start on day `D1`, and in general lasting to the following day `D2`). Then the different columns will be the time range where the different telescopes (codename written in the header column) can observe, in the format `HHMM-HHMM` (note no `:` in between HH and MM). Or blank or  dashes when they cannot observe. The last column (header `sched/comments` contains the name of the person that will be responsible to verify these observations; or everyone if not defined).

### planobs integration

`planobs` is imported as a Python library (not called as a subprocess). It is used in two modes:

1. **Visibility query**: given a list of sources (each with coordinates, phasecal, and checksource from `targets.toml`), an observation window, and a list of antenna names, returns a per-source, per-15-minute-slot observation count (number of antennas that can observe). It will need to do the following call:
   `planobs -t TARGETS... -sc path-to-targets.toml -s STATIONS... -t1 'YYYY-MM-DD HH:MM' -d DURATION`, where `TARGETS` will be the list with the names of the targets to potentially observe. You will need to use the Python module to call this and then get the output.
2. **Schedule generation**: given the confirmed source list and observation window, generates and writes the `.key` schedule file to a specified output path.

The precise API calls will be determined by reading `planobs` source/docs; this PRD does not hard-code them.

### Source selection logic

The scheduler scores each target with `priority >= 1` (priority-0 sources are always excluded) as follows:

- Sources with `priority == 2` are automatically included in the candidate set.
- Sources with `priority == 1` are included only if they appear in the activity database with a detection within the last 30 days.
- From the candidate set, sources with no visibility (all-antenna overlap zero across the entire window) are excluded.
- The final proposal selects up to four sources, ranked by: recency of last detection (most recent first), then by priority descending, then alphabetically as a tiebreaker.

This logic is configurable (thresholds as named constants, not magic numbers).

### Slack interaction design

The daemon uses the Slack Web API (Bot Token) to post Block Kit messages to a designated PRECISE Slack channel. The proposal message includes:

- A text section listing FRBs active in the last 15 days and in the last 30 days.
- A visibility table rendered as a Slack Block (emoji grid: 🟩 = all antennas, 🟨 = all-but-one, ⬜ = not observable, per 15-min slot).
- The proposed source list with per-source metadata.
- A table with the last PRECISE observations and the FRB sources that were observed in there, and how much time were observed.
- A "Confirm" button (accepts the proposal as-is).
- A multi-select dropdown listing all candidate sources (for overriding the proposal), plus a "Submit override" button.

The daemon listens for Slack interactive payloads via an HTTP endpoint (Slack sends a POST to a configured webhook URL). On receipt of a valid confirmation, it proceeds to schedule generation. If no response 5 days before observations, it re-posts a reminder in the same thread. After the reminder, it waits indefinitely, but posts an escalation alert if fewer than five days remain before the observation.

### Git workflow

The daemon operates on a clone of the PRECISE observations repository (or a designated sub-directory within `precise_schedules`). It commits the generated `.key` file directly to `main` in the `precise_schedules` repository with the message `"Draft schedule for <PROJECT>"` where `<PROJECT>` is the project code from Google Sheets. On finalisation, it pushes a second commit `"Final schedule for <PROJECT>"`. No pull requests are used.

### Upload mechanism

Finalisation triggers a `scp` call to copy the compiled schedule files to a configured path (`vlbi_arch/mmmYY/`) on the `vlbeer.ira.inaf.it` server, using SSH key authentication. Success/failure is reported to Slack, and it will notify stations with a message in the appropriate Mattermost channel.  Both Mattermost and Slack channel IDs and configurations will be written in a config file in the machine (following standard practices under `~/.config/precise/`, or if defined following the `XDG..` variable, or at `$HOME`).

---

## Module Design

### Activity

- **Responsibility**: owns all reads and writes to the activity database file.
- **Python Interface**:
  - `add_detection(canonical_name: str, utc_datetime: datetime, facility: str = "CHIME") -> None`
  - `get_active_sources(since_days: int) -> list[str]` — returns canonical names with at least one detection in the window.
  - `get_detections(canonical_name: str, since_days: int) -> list[Detection]` — returns records for a single source.
  - `delete_detection(canonical_name: str, utc_datetime: datetime) -> bool`
- **CLI interface:**
  - `precise add-detection  --source cannonical_name --utc 'DD-MM-YYYY HH:MM[:SS.SSSSS]'  [-f/--facility CHIME]`
  - `precise activity [--since XXd/Xmonth/Xyr] [--source XX]` it calls both `get_active_sources` or `get_detections` depending on the parameters.
  - `precise delete-detection --source cannonical_name --utc DATETIME`

- **Failure modes**: file not found (initialise empty); write conflict (file-level lock or atomic write-then-rename).
- **Tested**: yes — unit tests with a temp file; cover add, query, and edge cases (empty DB, unknown source, future date).

### VOEventIngester

- **Responsibility**: parses incoming VOEvent XML and fans out to Slack posting and Activity.
- It will be implemented manually as it is already available in an external package.
- Internally extracts `canonical_name`, `utc_datetime`, and `facility` from the VOEvent; delegates posting to the existing Slack notifier; calls `Activity.add_detection()`.



### ObservationSchedules

* **Responsability**: reads the past PRECISE observations (parse the `.vex` files from the `precise_schedules` repository), keeping a list of all projects, the FRBs that were observed in each run, and how much time was spent on each source, and which antennas did observe.
* **Interface**:
  * `get_record(project: str = None, source: str = None, max_projects: int = None, max_time: str = None)` — returns the list with all PRECISE runs (up to `max_projects` or all runs observed earlier than `max_time` time ago) that included the `source` (if given, otherwise all), containing the source(s) observed or specified and the total time spent on them, with all antennas that where scheduled. If a `project` is defined, then it will return the data of only that project.
* **Failure modes**: `.vex` files not found. Error parsing them; no observation in the range; no observation with that source or project name.
* **Tested**: yes – integration test with a real folder with  `.vex` files.

### ObservationCalendar

- **Responsibility**: reads the PRECISE Google Sheets calendar and returns upcoming observation windows.
- **Interface**:
  - `get_next_observation() -> ObsWindow | None` — returns the next scheduled observation, or `None` if none found within 30 days.
  - `ObsWindow`: dataclass with `start: datetime`, `end: datetime`, `antennas: list[str]`, `project: str`.
- **Failure modes**: authentication error; sheet unavailable; no upcoming observation; malformed row.
- **Tested**: yes — integration test with a real sheet (read-only service account); mock test for error paths.

### VisibilityPlanner

- **Responsibility**: wraps `planobs` for visibility queries and schedule file generation.
- **Interface**:
  - `get_visibility_grid(targets: list[Target], obs_window: ObsWindow) -> VisibilityGrid`
    - `VisibilityGrid`: dict mapping `target.name` to a list of per-15-min-slot antenna counts.
  - `generate_schedule(targets: list[Target], obs_window: ObsWindow, output_path: Path) -> Path`
- **Failure modes**: `planobs` raises; no targets visible; output path not writable.
- **Tested**: yes — unit tests with mock `planobs`; integration smoke test against real `planobs` with a known source.

### SourceSelector

- **Responsibility**: applies priority + activity + visibility rules to produce a ranked source proposal.
- **Interface**:
  - `propose(targets: list[Target], activity_store: ActivityStore, visibility_grid: VisibilityGrid, max_sources: int = 4) -> list[Target]`
- Pure function (no side effects); all thresholds as named constants.
- **Tested**: yes — unit tests covering all priority combinations, empty activity, zero-visibility edge cases.

### SlackScheduler

- **Responsibility**: owns the Slack proposal message lifecycle — posting, interactive payload handling, reminders, and escalation.
- **Interface**:
  - `post_proposal(obs_window: ObsWindow, visibility_grid: VisibilityGrid, proposed: list[Target], active_15d: list[str], active_30d: list[str]) -> str` — returns Slack message timestamp.
  - `await_confirmation(message_ts: str, candidates: list[Target]) -> list[Target]` — blocks until a valid confirmation or override is received; sends reminder 5 days before the observation; keeps waiting another 48 h and cancels automatically.
- Internally runs a lightweight HTTP server (e.g. Flask or FastAPI) to receive Slack interactive payloads.
- **Tested**: yes — unit tests with mocked Slack API; test confirm path, override path, unauthorised user rejection, reminder trigger.

### SchedulePublisher

- **Responsibility**: commits schedule files to git and uploads to the antenna server.
- **Interface**:
  - `commit_schedule(path: Path = None, project: str, draft: bool = True) -> None` commits and pushes the .key and .vex file for the given project. They should be either in `path` (if specified) or in the current directory.
  - `compile_schedule(key_file: Path, path: Path = None)` –  compiles the schedule key file with `sched.py` in the given `path`, or current directory otherwise.
  - `upload_and_report(path: Path, project: str) -> None` - uploads all the files belonging to the project generated by pySCHED to `vlbeer` and sends the notifications.
- Uses `subprocess` for `git` and `scp`; it should upload both the `.key` and the `.sum` files. Before `push`, it should always `pull` before to synchronize possible changes.
- **Failure modes**: git push conflict; SSH auth failure; remote path permission denied.
- **Tested**: yes — unit tests with a local bare git repo and a local rsync target; mock SSH for upload tests.

### ScheduleDaemon

- **Responsibility**: top-level orchestrator; wires all modules together and runs the ten-day polling loop.
- **Interface**: single entry point (`main()`) invoked by the process manager (systemd or cron — TBD).
- Polls `ObservationCalendar` on a configurable interval (default: every 6 hours). When a window within 10 days is found, runs the full pipeline once per observation; uses a local state file to avoid re-triggering for the same observation.
- **Tested**: integration test covering the happy path end-to-end with mocked external dependencies.

---

## Testing Decisions

Tests validate external behaviour, not implementation details. The test suite lives in `tests/` and mirrors the module structure.

Key conventions:
- `Activity` tests use `tmp_path` (pytest fixture) for isolation.
- `VisibilityPlanner` tests mock `planobs` at the import boundary, plus one integration smoke test gated behind a `pytest` marker (`--run-integration`).
- `SlackScheduler` tests mock the Slack Web API client and drive the HTTP server with a test client.
- `SchedulePublisher` tests use `tmp_path` for the git repo and a local directory as the rsync target.
- `ScheduleDaemon` integration test uses a fixture that stubs all I/O and asserts on the sequence of calls.

Prior art: none in the current repo (greenfield). Use `pytest` with `pytest-mock` and `pytest-asyncio` if async paths are introduced.

---

## Out of Scope

- Post-observation data pull and pipeline triggering (described as existing automated behaviour; not modified here).
- Scheduling of non-repeating or non-FRB sources (e.g. calibrators beyond what `planobs` already handles internally).
- A web UI or dashboard; Slack is the only human interface.
- Multi-project or multi-telescope-array support beyond the PRECISE antenna set.
- Automatic ingestion of non-VOEvent activity reports (e.g. ATels, GCN circulars) — those must be added manually to the activity database.
- Editing `targets.toml` entries via Slack or any automated interface.

---

## Open Questions

1. **Google Sheets column layout** — the exact column names and sheet tab name are needed before `ObservationCalendar` can be implemented. *Owner: Benito. Resolution: sheet at https://docs.google.com/spreadsheets/d/14Pntaz1FvsYyAOzJYjqHk3UpLzJ2gsbBzWrIqMffxYo/edit?pli=1&gid=1799963561#gid=1799963561*
2. **planobs API** — the precise function signatures for visibility query and schedule generation need to be confirmed by reading the `planobs` source. *Owner: developer implementing `VisibilityPlanner`. Resolution: read `planobs` docs/source before implementation.*
3. **Slack webhook endpoint hosting** — the `SlackScheduler` needs an HTTP endpoint reachable from Slack's servers. This requires either a public IP/DNS on the group server or a tunnel (e.g. ngrok). *Owner: Benito / sysadmin. Resolution: confirm server connectivity or plan a tunnel before implementing `SlackScheduler`.*
4. **Daemon deployment model** — systemd service vs. cron job is left open. *Owner: Benito. Resolution: allow both and it will be decided during deployment.*
5. **Activity database format (CSV vs. TOML)** — both were mentioned as acceptable. TOML is more human-readable for structured records; CSV is simpler to append programmatically. *Owner: developer implementing `ActivityStore`. *
6. *Resolution: use CSV with a header row for simplicity and easy `pandas`/`csv` manipulation.*
7. **`targets.toml` location relative to `precise_schedules`** — `targets.toml` lives in a separate observations repository. The daemon needs a reliable path to it (git submodule, symlink, or configured path). *Owner: Benito. Resolution: define the path in the precise config file, and can be overwritten if called the functions with the path*

---

## Further Notes

- The `targets.toml` fields `phasecal` and `checksource` are passed through to `planobs` unchanged; they are not interpreted by any scheduler module other than `VisibilityPlanner`.
- The visibility grid colour scheme (🟩 / 🟨 / ⬜) maps to: all *N* antennas observable → green; *N−1* antennas observable → amber; fewer → blank. The value of *N* is determined at runtime from `ObsWindow.antennas`.
- The activity database should be pre-populated with historical detections before the daemon goes live, so that source scoring is meaningful from day one. This is a one-time manual step.
- The daemon must not re-trigger the scheduling pipeline for an observation it has already processed. A local state file (e.g. `state.json` keyed by observation start time) is the simplest guard.
- All times are stored and processed in UTC throughout. Local time is never used internally.
