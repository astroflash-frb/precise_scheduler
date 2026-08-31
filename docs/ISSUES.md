# PRECISE Scheduler — Implementation Issues

Issues are ordered by dependency (blockers first). Cross-references use issue numbers.

---

## Issue 1: Project scaffold & config loader

**Type**: AFK
**Blocked by**: None — can start immediately

### Parent PRD

`docs/PRD.md`

### What to build

Set up the Python package skeleton and the configuration subsystem that every other module depends on.

- A `pyproject.toml` defining the `precise-scheduler` package, its dependencies, and a `precise` CLI entry point with subcommand routing.
- A `Config` module that reads credentials and path settings from `~/.config/precise/config.toml` (falling back to `$XDG_CONFIG_HOME/precise/config.toml` then `$HOME/.precise/config.toml`). Config keys include: path to `targets.toml`, path to the `precise_schedules` repository clone, path to the activity CSV file, Slack bot token, Slack channel ID, Mattermost webhook URL, antenna server SSH details, and Google Sheets credentials file path.
- A logging setup (structured log to a rotating file, warning+ to stderr) shared across all modules.
- Stub subcommand handlers (`precise <subcommand>`) that print "not implemented" — real logic is filled in by later issues.

The config file must never be committed to the repository; a `config.toml.example` with placeholder values is committed instead.

### How to verify

- **Manual**: `pip install -e .` succeeds; `precise --help` lists known subcommands; `precise activity` prints "not implemented".
- **Automated**: test that `Config` raises a clear `ConfigError` when the file is missing; test that each XDG/fallback path is tried in order.

### Acceptance criteria

- [ ] `precise --help` runs without error after a clean install.
- [ ] `Config` loads a valid config file and exposes all required keys as typed attributes.
- [ ] `Config` raises `ConfigError` with a human-readable message if the config file is absent or a required key is missing.
- [ ] A `config.toml.example` is committed to the repo with placeholder values for every required key.
- [ ] Log output goes to a rotating file and to stderr (≥ WARNING) without additional caller setup.

### User stories addressed

- Prerequisite for all user stories.

---

## Issue 2: Activity module — CSV backend and CLI

**Type**: AFK
**Blocked by**: Issue 1

### Parent PRD

`docs/PRD.md`

### What to build

Implement the `Activity` module and expose it through three `precise` CLI subcommands. This is the first end-to-end slice: a user can add, query, and delete detection records entirely from the command line, and the CSV file can be inspected in a text editor or diffed in git.

**Module** (`precise_scheduler/activity.py`):
- `add_detection(canonical_name: str, utc_datetime: datetime, facility: str = "CHIME") -> None` — appends a row to the CSV; creates the file with a header if it does not exist; uses an atomic write-then-rename to avoid partial writes.
- `get_active_sources(since_days: int) -> list[str]` — returns canonical names that have at least one record within `since_days` of now (UTC).
- `get_detections(canonical_name: str, since_days: int) -> list[Detection]` — returns all records for one source within the window, newest first.
- `delete_detection(canonical_name: str, utc_datetime: datetime) -> bool` — removes the matching row; returns `True` if found and deleted.
- `Detection`: dataclass with `canonical_name`, `utc_datetime`, `facility`.

**CSV schema**: header row `canonical_name,utc_datetime,facility`; `utc_datetime` stored as ISO 8601 with timezone `+00:00`.

**CLI subcommands**:
- `precise add-detection --source CANONICAL_NAME --utc 'DD-MM-YYYY HH:MM[:SS.ffffff]' [-f/--facility CHIME]`
- `precise activity [--since Nd|Nmonth|Nyr] [--source CANONICAL_NAME]` — with `--source` calls `get_detections`; without calls `get_active_sources`; default `--since 30d`.
- `precise delete-detection --source CANONICAL_NAME --utc DATETIME`

### How to verify

- **Manual**:
  1. `precise add-detection --source FRB20201124A --utc '01-07-2026 12:00' -f CHIME` → exits 0.
  2. `precise activity --source FRB20201124A` → shows the record.
  3. `precise activity --since 15d` → lists FRB20201124A (detection is within 15 days of test run).
  4. `precise delete-detection --source FRB20201124A --utc '01-07-2026 12:00'` → exits 0.
  5. `precise activity --source FRB20201124A` → shows no records.
- **Automated**: unit tests using `tmp_path`; cover: add to new file, add to existing file, query empty DB, query unknown source, delete non-existent row, future datetime, `since` boundary (on the edge of the window).

### Acceptance criteria

- [ ] Given a new activity CSV path, `add_detection` creates the file with a header row.
- [ ] Given an existing file, `add_detection` appends without overwriting.
- [ ] Given a detection added 14 days ago, `get_active_sources(since_days=15)` returns it; `get_active_sources(since_days=13)` does not.
- [ ] `delete_detection` returns `False` for an unknown record; the file is unchanged.
- [ ] `precise activity --since 1d` prints a human-readable table to stdout.
- [ ] All datetimes are stored and compared in UTC; no local-time conversion occurs.

### User stories addressed

- User story 2: manually add historical detections.
- User story 3: correct or delete erroneous records.
- User story 4: query the activity database.
- User story 5: record non-CHIME detections with facility label.

---

## Issue 3: targets.toml schema extension

**Type**: HITL
**Blocked by**: Issue 1

### Parent PRD

`docs/PRD.md`

### What to build

Extend every entry in `targets.toml` with two new required fields (`priority` and `canonical_name`) and write the loader that validates and parses the file.

**Schema additions** (per `[[target]]` entry):
- `priority`: integer, 0–2. 0 = no new observations needed; 1 = observe only if active in last 30 days; 2 = high scientific priority.
- `canonical_name`: string matching the CHIME/TNS identifier (e.g. `"FRB20200223D"`), used as the join key with the activity database.

**Loader** (`precise_scheduler/targets.py`):
- `load_targets(path: Path) -> list[Target]` — parses `targets.toml`, validates all required fields, and returns typed `Target` dataclasses. Raises `TargetSchemaError` listing every entry missing `priority` or `canonical_name`.
- `Target`: dataclass mirroring the full entry (name, coordinates, phasecal, checksource, priority, canonical_name).

The HITL step: a team member must assign correct `priority` and `canonical_name` values to all existing entries in `targets.toml` before the loader integration test can pass. This requires scientific judgement and is not automatable.

### How to verify

- **Manual**: after filling in all entries, `python -c "from precise_scheduler.targets import load_targets; print(load_targets('path/to/targets.toml'))"` returns a list with no errors.
- **Automated**: unit tests with synthetic TOML strings; cover: valid entry, missing `priority`, missing `canonical_name`, both missing, `priority` out of range (e.g. 3).

### Acceptance criteria

- [ ] `load_targets` returns a `Target` for every entry when all fields are present and valid.
- [ ] `load_targets` raises `TargetSchemaError` (listing offending entry names) if any entry is missing `priority` or `canonical_name`.
- [ ] `priority` values outside 0–2 raise `TargetSchemaError`.
- [ ] All pre-existing fields (`name`, `coordinates`, `phasecal`, `checksource`) are preserved and accessible on `Target`.
- [ ] **HITL**: all entries in the real `targets.toml` have been filled in and `load_targets` runs against it without error.

### User stories addressed

- User story 6: assign priority labels.
- User story 7: `canonical_name` field for VOEvent matching.
- User story 8: add a new target without touching other config files.

---

## Issue 4: ObservationCalendar — Google Sheets reader

**Type**: AFK
**Blocked by**: Issue 1

### Parent PRD

`docs/PRD.md`

### What to build

Implement `ObservationCalendar` to read the PRECISE scheduling spreadsheet and return the next upcoming observation window.

**Spreadsheet layout** (from PRD Implementation Decisions):
- One tab per month, named `mmmYY` (e.g. `Jul26`). Missing tab = no observations that month.
- Each observation is a row block: column 1 = project name; column 2 = date range (`D1-D2 Mmm`); remaining columns = per-telescope time ranges (`HHMM-HHMM`) with the telescope codename in the header; blank or dash = that telescope not participating.
- Last column (`sched/comments`) = responsible person.

**Module** (`precise_scheduler/calendar.py`):
- `ObsWindow`: dataclass with `start: datetime`, `end: datetime`, `antennas: list[str]`, `project: str`.
- `get_next_observation() -> ObsWindow | None` — reads the current and next month's tabs, finds the first observation whose start is in the future, returns it. Returns `None` if nothing found within 30 days.

Authentication: service account credentials file path from `Config`.

### How to verify

- **Manual**: with a valid service account credential, `python -c "from precise_scheduler.calendar import ObservationCalendar; print(ObservationCalendar().get_next_observation())"` prints a populated `ObsWindow`.
- **Automated**: unit tests with a mocked Sheets API response covering: normal row, blank telescope column (not participating), missing tab (no observations that month), all observations in the past (returns `None`), malformed date cell.

### Acceptance criteria

- [ ] `get_next_observation` returns an `ObsWindow` with correct `start`, `end`, `antennas`, and `project` for the next scheduled observation.
- [ ] Telescopes with blank or dash time cells are excluded from `ObsWindow.antennas`.
- [ ] Returns `None` if no observation falls within the next 30 days.
- [ ] Raises a logged `CalendarError` (does not crash) if the Sheets API is unreachable or the credential is invalid.
- [ ] All datetimes in `ObsWindow` are UTC-aware.

### User stories addressed

- User story 9: automatic trigger 10 days before each observation (calendar prerequisite).

---

## Issue 5: VisibilityPlanner — planobs wrapper

**Type**: AFK
**Blocked by**: Issue 3, Issue 4

### Parent PRD

`docs/PRD.md`

### What to build

Wrap the `planobs` Python library to expose two operations: a visibility grid query and schedule file generation.

**Module** (`precise_scheduler/visibility.py`):
- `VisibilityGrid`: `dict[str, list[int]]` mapping target internal name to a list of per-15-min-slot antenna counts (length = number of 15-min slots in the observation window).
- `get_visibility_grid(targets: list[Target], obs_window: ObsWindow) -> VisibilityGrid` — calls `planobs` with the source list, station list, start time, and duration; parses output into a `VisibilityGrid`.
- `generate_schedule(targets: list[Target], obs_window: ObsWindow, output_path: Path) -> Path` — calls `planobs` to produce the `.key` file at `output_path`; returns the path.

Consult `planobs` source/docs to determine the exact API before implementing (see PRD open question #2). The call signature from the PRD is: `planobs -t TARGETS... -sc path-to-targets.toml -s STATIONS... -t1 'YYYY-MM-DD HH:MM' -d DURATION`.

### How to verify

- **Manual** (`--run-integration`): call `get_visibility_grid` with a known source (e.g. `FRB20201124A`) and a real `ObsWindow` covering a few hours. Verify the returned grid has the correct number of slots and non-zero counts for at least one slot.
- **Automated**: unit tests with `planobs` mocked at the import boundary; cover: normal output, empty source list, `planobs` exception, output path not writable.

### Acceptance criteria

- [ ] `get_visibility_grid` returns a grid with one entry per target and one integer per 15-min slot.
- [ ] A target with zero observable slots across the entire window has all-zero slot values.
- [ ] `generate_schedule` writes a `.key` file to the specified path and returns that path.
- [ ] Both functions raise `VisibilityError` (logged, not crashed) if `planobs` raises or returns unexpected output.
- [ ] Integration test (marked `--run-integration`) passes against real `planobs`.

### User stories addressed

- User story 11: visibility grid in the Slack message.
- User story 18: `.key` file generated automatically after confirmation.

---

## Issue 6: SourceSelector — scoring and ranking

**Type**: AFK
**Blocked by**: Issue 2, Issue 3, Issue 5

### Parent PRD

`docs/PRD.md`

### What to build

Implement the pure scoring function that converts targets + activity data + visibility into a ranked proposal.

**Module** (`precise_scheduler/selector.py`):
- `propose(targets: list[Target], activity: Activity, visibility_grid: VisibilityGrid, max_sources: int = 4) -> list[Target]`

**Rules** (all thresholds as named module-level constants):
1. Exclude `priority == 0` sources unconditionally.
2. Exclude `priority == 1` sources with no detection in the last `ACTIVITY_WINDOW_DAYS` (default 30).
3. Exclude sources with zero observable slots across the full window.
4. From the remaining candidates, rank by: days since last detection (ascending), then `priority` (descending), then `canonical_name` (alphabetical tiebreaker).
5. Return the top `max_sources`.

The function is pure — no file I/O, no Slack calls, no side effects. It receives pre-fetched `Activity` and `VisibilityGrid` objects.

### How to verify

- **Automated**: unit tests covering:
  - Priority-0 source always excluded.
  - Priority-1 source excluded when last detection > 30 days ago, included when ≤ 30 days.
  - Priority-2 source included regardless of activity.
  - Zero-visibility source excluded even if priority-2.
  - Ranking: more-recently-active source ranked above less-recent.
  - `max_sources` cap respected.
  - Empty target list returns empty list.
  - All candidates excluded returns empty list.

### Acceptance criteria

- [ ] Priority-0 sources never appear in the output.
- [ ] Priority-1 sources appear only when they have a detection within `ACTIVITY_WINDOW_DAYS`.
- [ ] Priority-2 sources appear regardless of activity, unless they have zero visibility.
- [ ] Output is sorted: most recent detection first, then priority descending, then alphabetical.
- [ ] Output length ≤ `max_sources`.
- [ ] All thresholds (`ACTIVITY_WINDOW_DAYS`, default `max_sources`) are named constants, not literals.

### User stories addressed

- User story 12: proposed source list with per-source reasoning.

---

## Issue 7: ObservationSchedules — VEX file parser

**Type**: AFK
**Blocked by**: Issue 1

### Parent PRD

`docs/PRD.md`

### What to build

Parse historical `.vex` files from the `precise_schedules` repository clone to produce a per-project record of sources observed, time on source, and participating antennas.

**Module** (`precise_scheduler/obs_schedules.py`):
- `ObsRecord`: dataclass with `project: str`, `start: datetime`, `sources: list[SourceTime]`.
- `SourceTime`: dataclass with `name: str`, `total_seconds: float`, `antennas: list[str]`.
- `get_records(repo_path: Path, project: str = None, source: str = None, max_projects: int = None, max_age: timedelta = None) -> list[ObsRecord]`
  - If `project` is given, return only that project's record.
  - If `source` is given, filter to records that include that source.
  - `max_projects`: cap the number of returned records (most recent first).
  - `max_age`: exclude records older than `max_age` from now.

VEX files are found by globbing `**/*.vex` under `repo_path`. The parser must handle the standard VEX 1.5 format: extract `$SCHED` blocks to find scan start times and source names, and `$SOURCE` blocks for source identification.

### How to verify

- **Manual**: point `get_records` at a folder containing real `.vex` files; verify the returned `ObsRecord` list matches known observation history.
- **Automated**: integration test with a fixtures folder containing 2–3 synthetic or anonymised `.vex` files; assert correct project name, source names, and total time per source.

### Acceptance criteria

- [ ] `get_records` with no filters returns all projects found in `.vex` files, newest first.
- [ ] `max_projects=5` returns at most 5 records.
- [ ] Filtering by `source` excludes projects that did not observe that source.
- [ ] `SourceTime.total_seconds` matches the sum of scan durations for that source across the project.
- [ ] Missing or malformed `.vex` files are skipped with a logged warning; they do not raise.
- [ ] Returns an empty list (not an error) when no `.vex` files are found.

### User stories addressed

- User story 13: previous PRECISE runs and time-on-source shown in Slack message.

---

## Issue 8: Slack proposal message (static content)

**Type**: AFK
**Blocked by**: Issue 6, Issue 7

### Parent PRD

`docs/PRD.md`

### What to build

Post the full Block Kit proposal message to Slack. This issue covers content and formatting only — interactive buttons and payload handling are added in Issue 9.

**Module** (`precise_scheduler/slack_scheduler.py`), function `post_proposal`:
- `post_proposal(obs_window: ObsWindow, visibility_grid: VisibilityGrid, proposed: list[Target], active_15d: list[str], active_30d: list[str], recent_runs: list[ObsRecord]) -> str`  — posts to the configured Slack channel and returns the message timestamp (`ts`).

**Message sections** (Block Kit):
1. Header: observation date range, project code, participating antennas.
2. Activity summary: FRBs detected in the last 15 days (with detection count); FRBs detected in the last 30 days (with detection count).
3. Previous runs table: last ≤5 PRECISE runs or last 3 months (whichever is shorter), showing project, sources observed, and time on each source.
4. Visibility grid: emoji table (🟩 = all antennas, 🟨 = all-but-one, ⬜ = not observable) per 15-min slot per candidate source.
5. Proposed source list: one line per source with `priority`, days since last detection, and visibility fraction.
6. Placeholder text: "Confirmation controls will appear here." (replaced by Issue 9).

### How to verify

- **Manual**: run `post_proposal` with realistic fixture data; verify the message appears in the correct Slack channel with all five sections readable.
- **Automated**: unit tests with mocked `slack_sdk`; assert the Block Kit payload contains the correct section types and that the emoji grid encodes the visibility counts correctly (all-N → 🟩, N-1 → 🟨, <N-1 → ⬜).

### Acceptance criteria

- [ ] Message appears in the correct Slack channel within 5 seconds of the call.
- [ ] Each of the five sections is present and non-empty.
- [ ] Emoji grid has one column per 15-min slot and one row per candidate source.
- [ ] `post_proposal` returns a non-empty message `ts` string.
- [ ] If the Slack API call fails, a `SlackError` is raised with the API error message.

### User stories addressed

- User story 9: automatic Slack message before each observation.
- User story 10: FRBs active in last 15 and 30 days shown.
- User story 11: visibility grid in the message.
- User story 12: proposed sources with reasoning.
- User story 13: previous runs table.

---

## Issue 9: Slack interactive confirmation

**Type**: HITL
**Blocked by**: Issue 8

### Parent PRD

`docs/PRD.md`

### What to build

Add interactive Block Kit controls to the proposal message and implement the HTTP endpoint that receives and validates Slack's interactive payload callbacks.

**HITL prerequisite**: a Slack app must be created, configured with a public-facing Request URL for interactive components, and the bot token added to the config. The server hosting the daemon must have the endpoint reachable from Slack's servers (public IP, DNS, or tunnel — see PRD open question #3).

**Changes to `slack_scheduler.py`**:
- Replace the placeholder section in `post_proposal` with a "Confirm" button and a multi-select source dropdown + "Submit override" button.
- `await_confirmation(message_ts: str, candidates: list[Target], obs_window: ObsWindow) -> list[Target]` — starts the HTTP server, blocks until a valid payload arrives or the observation has passed.
  - Authorisation: only Slack users whose email is in the configured `authorised_users` list may confirm or override; others receive an ephemeral "not authorised" reply.
  - Reminder: if no response is received by 5 days before the observation start, re-post a reminder in the same thread.
  - After the reminder, waits indefinitely; posts an escalation alert (see Issue 13) if fewer than 5 days remain.
- The HTTP server is a lightweight FastAPI or Flask app; it validates Slack's request signature before processing any payload.

### How to verify

- **Manual** (HITL): post a real proposal to a test Slack channel; click "Confirm" as an authorised user → `await_confirmation` returns the proposed list. Click "Submit override" with a different source selection → returns the overridden list. Attempt to confirm as an unauthorised user → ephemeral "not authorised" appears.
- **Automated**: unit tests with a mocked Slack API and a test HTTP client; cover confirm path, override path, unauthorised user rejection, and reminder trigger (mock `datetime.now`).

### Acceptance criteria

- [ ] Clicking "Confirm" as an authorised user resolves `await_confirmation` with the proposed source list.
- [ ] Submitting an override resolves `await_confirmation` with the selected sources.
- [ ] An unauthorised user receives an ephemeral "not authorised" message; the wait continues.
- [ ] Slack request signatures are verified; unsigned or invalid requests return HTTP 401.
- [ ] A reminder is posted in the thread when the 5-day-before threshold is crossed.
- [ ] The HTTP endpoint is shut down cleanly after confirmation is received.

### User stories addressed

- User story 14: confirm with a single button click.
- User story 15: override via dropdown.
- User story 16: reminder 5 days before the observation.
- User story 17: human confirmation always required.
- User story 18 (shift lead auth): only authorised users can confirm.

---

## Issue 10: Schedule generation and draft git commit

**Type**: AFK
**Blocked by**: Issue 9

### Parent PRD

`docs/PRD.md`

### What to build

On receipt of a confirmed source list from Issue 9, generate the `.key` schedule file and commit it to the `precise_schedules` repository.

**Module** (`precise_scheduler/publisher.py`):
- `compile_schedule(key_file: Path, output_path: Path = None) -> Path` — runs `sched.py -k <key_file>` in `output_path` (or current directory if `None`); returns the path to the generated output directory.
- `commit_schedule(path: Path = None, project: str, draft: bool = True) -> None` — stages the `.key` (and `.vex` if present) file(s) for the given project; pulls from `origin/main` before committing; commits with `"Draft schedule for <PROJECT>"` (if `draft=True`) or `"Final schedule for <PROJECT>"`; pushes to `origin/main`.

The flow in this issue: `VisibilityPlanner.generate_schedule()` is called with the confirmed sources → `.key` file written → `commit_schedule(..., draft=True)` committed and pushed.

### How to verify

- **Manual**: after Slack confirmation, inspect the `precise_schedules` repository; a new commit `"Draft schedule for <PROJECT>"` should appear on `main` containing the `.key` file.
- **Automated**: unit tests using a local bare git repository (`tmp_path`); assert the commit message, that the `.key` file is in the commit tree, and that `pull` is called before `push`.

### Acceptance criteria

- [ ] After confirmation, the `.key` file appears in the `precise_schedules` repo on `main`.
- [ ] The commit message is exactly `"Draft schedule for <PROJECT>"`.
- [ ] A `git pull` is performed before the commit to avoid divergence.
- [ ] If `git push` fails (e.g. conflict), the error is raised as `PublisherError` and logged; no partial state is left.
- [ ] The committed `.key` file is identical to the one generated by `planobs`.

### User stories addressed

- User story 18: `.key` file generated automatically.
- User story 19: draft committed to the repository.
- User story 20: team can review the committed file before upload.

---

## Issue 11: Finalisation — compile, upload to vlbeer, and announce

**Type**: AFK
**Blocked by**: Issue 10

### Parent PRD

`docs/PRD.md`

### What to build

Implement the finalisation command that a team member triggers (via Slack slash command or CLI) after reviewing the draft schedule. It compiles the schedule, uploads all artefacts to the antenna server, pushes the final git commit, and notifies both Slack and the Mattermost station channel.

**Additions to `publisher.py`**:
- `upload_and_report(repo_path: Path, project: str) -> None`:
  1. `git pull` the latest `precise_schedules` clone to ensure the reviewed `.key` is current.
  2. Run `sched.py -k *.key` to compile; collect all output files (`.key`, `.sum`, and any other pySCHED outputs).
  3. `scp` all output files to `vlbeer.ira.inaf.it:vlbi_arch/mmmYY/` (month/year derived from the observation start time) using the SSH key from config.
  4. Call `commit_schedule(..., draft=False)` to push `"Final schedule for <PROJECT>"`.
  5. Post a Slack success message in the scheduling channel.
  6. Post the antenna notification message to the configured Mattermost channel webhook.

**CLI**: `precise distribute --project <PROJECT>` (invokes `upload_and_report`).

### How to verify

- **Manual**: run `precise distribute --project <PROJECT>` after reviewing a draft; verify the files appear at `vlbeer.ira.inaf.it:vlbi_arch/mmmYY/`; verify the Slack success message; verify the Mattermost post in the station channel; verify the final git commit on `main`.
- **Automated**: unit tests with a local directory as the SCP target (mocked `subprocess`); assert all output files are copied, the final commit message is correct, and both notification calls are made.

### Acceptance criteria

- [ ] All pySCHED output files (at minimum `.key` and `.sum`) are present at the correct path on `vlbeer` after `upload_and_report`.
- [ ] The final git commit message is `"Final schedule for <PROJECT>"`.
- [ ] A Slack message confirming the upload is posted to the scheduling channel.
- [ ] A notification is posted to the Mattermost station channel.
- [ ] If the SCP transfer fails, `PublisherError` is raised; no final commit is pushed and both notification calls are skipped.
- [ ] `precise distribute --project <PROJECT>` invokes the full sequence end-to-end.

### User stories addressed

- User story 21: trigger final upload via a single command.
- User story 22: final git commit pushed on upload.
- User story 23: compiled files uploaded to `vlbeer.ira.inaf.it/vlbi_arch/mmmYY/`.
- User story 24: Slack confirmation on upload success.
- User story 25: Mattermost station notification.

---

## Issue 12: VOEvent ingester — extend to write activity database

**Type**: AFK
**Blocked by**: Issue 2

### Parent PRD

`docs/PRD.md`

### What to build

Extend the existing VOEvent-to-Slack handler (external package) to also call `Activity.add_detection()` after each successful Slack post. The extension is additive — no existing Slack posting behaviour is modified.

Add an integration point (a callback or hook) in the existing handler so that `Activity.add_detection(canonical_name, utc_datetime, facility)` is called with fields extracted from the parsed VOEvent XML. The `canonical_name` is extracted from the VOEvent's IVORN or `Param` fields; `facility` defaults to `"CHIME"` unless the VOEvent origin indicates otherwise.

Document how to configure this extension in the existing handler (likely a config flag or plugin registration call).

### How to verify

- **Manual**: feed a real CHIME VOEvent XML into the handler; verify both the Slack message appears and a new row is written to the activity CSV.
- **Automated**: unit test with a synthetic VOEvent XML fixture; mock the Slack notifier; assert `Activity.add_detection` is called with the correct arguments.

### Acceptance criteria

- [ ] Processing a CHIME VOEvent writes exactly one row to the activity CSV.
- [ ] The `canonical_name` in the CSV matches the source identifier in the VOEvent.
- [ ] The `facility` is `"CHIME"` for standard CHIME VOEvents.
- [ ] If `Activity.add_detection` raises, the error is logged and the Slack post still completes (fail-safe).
- [ ] No existing Slack posting behaviour is altered.

### User stories addressed

- User story 1: VOEvents recorded in activity database automatically.

---

## Issue 13: Error handling and alerting

**Type**: AFK
**Blocked by**: Issue 8

### Parent PRD

`docs/PRD.md`

### What to build

Add Slack error alerts for all daemon failure modes defined in the PRD, plus persistent structured logging across all modules.

**Failure modes to handle** (each posts a distinct Slack alert to the scheduling channel):
- `CalendarError` from `ObservationCalendar`: "Could not read the Google Sheets calendar. Manual check required."
- `VisibilityError` from `VisibilityPlanner`: "planobs failed for <source list>. Please check the planobs configuration."
- `PublisherError` from `SchedulePublisher` (upload): "SCP upload to vlbeer failed for <PROJECT>. Manual upload required."
- Deadline alert: if no confirmation has been received and fewer than 5 days remain before the observation, post "⚠️ Schedule for <PROJECT> has not been confirmed — observation in <N> days."
- `PublisherError` (git push): "Git push failed for <PROJECT>. Check for conflicts on main."

**Logging**: every module already calls the shared logger from Issue 1. This issue ensures all `except` blocks log at `ERROR` level with a full traceback before raising or swallowing the exception.

**Helper** (`precise_scheduler/alerts.py`):
- `post_alert(message: str) -> None` — thin wrapper around the Slack API call for sending to the scheduling channel.

### How to verify

- **Automated**: unit tests that inject each exception type and assert `post_alert` is called with a message containing the expected keywords.
- **Manual**: temporarily break the Sheets credential and run the daemon poll; verify an alert appears in Slack.

### Acceptance criteria

- [ ] Each failure mode in the PRD produces a distinct, human-readable Slack alert.
- [ ] All `except` blocks log at `ERROR` with a traceback before calling `post_alert`.
- [ ] `post_alert` itself does not raise if the Slack API is down (logs and returns silently).
- [ ] The deadline alert fires when `now > obs_start - 5 days` and no confirmation has been received.

### User stories addressed

- User story 25: Slack alert on calendar read failure.
- User story 26: Slack alert on `planobs` failure.
- User story 27: Slack alert on upload failure.
- User story 28: escalation alert when deadline approaches without confirmation.
- User story 29: persistent log file for post-mortem diagnosis.

---

## Issue 14: ScheduleDaemon — polling loop and state tracking

**Type**: AFK
**Blocked by**: Issue 11, Issue 13

### Parent PRD

`docs/PRD.md`

### What to build

Implement the main daemon that ties all modules together in a polling loop.

**Module** (`precise_scheduler/daemon.py`):
- `main()` — entry point registered as the `precise daemon` CLI subcommand (and as the systemd/cron target).
- Loop: every `POLL_INTERVAL_HOURS` (default 6, configurable), call `ObservationCalendar.get_next_observation()`.
  - If no window within 10 days: sleep until next poll.
  - If a window is within 10 days and its `project` is already in `state.json` (keyed by `obs_window.start.isoformat()`): skip (already processed).
  - Otherwise: run the full pipeline — load targets → fetch activity → compute visibility → select sources → fetch recent runs → post Slack proposal → await confirmation → generate schedule → commit draft → record project in `state.json`.
- `state.json` is written atomically after a successful draft commit to prevent re-triggering.

**Deployment note**: the PRD leaves systemd vs. cron open; `main()` must work correctly invoked either as a long-running process or as a one-shot cron invocation (the state file provides idempotence in both cases).

### How to verify

- **Automated**: end-to-end integration test with all external I/O (Sheets, Slack, planobs, git, scp) mocked; assert the sequence of calls (calendar → targets → activity → visibility → selector → post → await → generate → commit) in order for an observation 8 days away; assert the second poll skips the same observation.
- **Manual**: run `precise daemon` on the server; inspect logs after 6 hours to confirm polling; inspect `state.json` after a full run.

### Acceptance criteria

- [ ] A single observation triggers the full pipeline exactly once, regardless of how many times the loop runs.
- [ ] `state.json` is written atomically; a crash mid-pipeline does not leave a partial state entry.
- [ ] Observations more than 10 days away do not trigger the pipeline.
- [ ] `POLL_INTERVAL_HOURS` is a named constant overridable via config.
- [ ] `precise daemon` starts without error and logs "Daemon started, polling every N hours" to both file and stderr.

### User stories addressed

- User story 9: automatic trigger 10 days before each observation (the daemon is the trigger).
