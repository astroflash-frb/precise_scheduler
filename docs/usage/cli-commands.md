# CLI Reference

PRECISE Scheduler provides a command-line interface for managing FRB detections and scheduling observations.

## Global Options

These options are available with all commands:

- `--config PATH` — Override the default config file location
- `--debug` — Debug mode: print all antenna/upload actions to terminal; never send anything to the antenna server
- `-h, --help` — Show help message and exit

## Commands

### add-detection

Record a new FRB detection in the activity database.

```bash
precise add-detection --source CANONICAL_NAME --utc DATETIME [-f FACILITY]
```

**Required Arguments:**

- `--source CANONICAL_NAME` — Canonical source name (e.g., `FRB20201124A`)
- `--utc DATETIME` — Detection time as `DD-MM-YYYY HH:MM[:SS.ffffff]` (UTC)

**Optional Arguments:**

- `-f, --facility FACILITY` — Detecting facility (default: `CHIME`)

**Examples:**

```bash
# Basic detection with default facility (CHIME)
precise add-detection --source FRB20201124A --utc "24-11-2020 18:41:02"

# With custom facility
precise add-detection --source FRB20201124A --utc "24-11-2020 18:41:02.123456" -f "CHIME"

# With microsecond precision
precise add-detection --source FRB20230101X --utc "01-01-2023 12:34:56.789012" -f "Arecibo"
```

### activity

Show FRB detections from the activity database.

```bash
precise activity [--since WINDOW] [--source CANONICAL_NAME]
```

**Optional Arguments:**

- `--since WINDOW` — Look-back window (e.g., `15d`, `2month`, `1yr`). Default: `30d`
- `--source CANONICAL_NAME` — Filter to a single source

**Examples:**

```bash
# Show all active sources in the last 30 days
precise activity

# Show all detections for a specific source in the last week
precise activity --source FRB20201124A --since 7d

# Show active sources in the last 3 months
precise activity --since 3month

# Show active sources in the last year
precise activity --since 1yr
```

**Output Format:**

When viewing all sources, output shows:

```
Canonical name
FRB20201124A
FRB20230101X
```

When filtering by source, shows a detailed table:

```
UTC datetime                  Facility
2020-11-24T18:41:02+00:00     CHIME
2020-11-25T12:30:15+00:00     Arecibo
```

### delete-detection

Remove a detection record from the activity database.

```bash
precise delete-detection --source CANONICAL_NAME --utc DATETIME
```

**Required Arguments:**

- `--source CANONICAL_NAME` — Canonical source name
- `--utc DATETIME` — Detection time as `DD-MM-YYYY HH:MM[:SS.ffffff]` (UTC)

**Examples:**

```bash
# Delete a detection
precise delete-detection --source FRB20201124A --utc "24-11-2020 18:41:02"

# Delete with microsecond precision
precise delete-detection --source FRB20201124A --utc "24-11-2020 18:41:02.123456"
```

### calendar

List upcoming observing runs from the Google Sheets calendar.

```bash
precise calendar [--days N] [--no-vlbeer]
```

Reads the scheduling spreadsheet and prints a table of the upcoming observing
runs: project code, date, time range (UT), the stations scheduled to
participate, and how far each run has got:

| column | ✓ means |
| --- | --- |
| `.key` | a schedule for this project exists in the schedules repository |
| `vlbeer` | a `.key` or `.vex` for it is already uploaded to vlbeer |

The vlbeer column needs one ssh connection, made once for all the months in
view rather than once per run. If vlbeer cannot be reached the column shows
`?` rather than `✗` — "cannot tell" and "not uploaded" are different answers,
and only one of them is a reason to act.

**Optional Arguments:**

- `--days N` — How many days ahead to look (default: `20`)
- `--no-vlbeer` — Skip the vlbeer check and its ssh connection.

**Examples:**

```bash
# Show observations in the next 20 days
precise calendar

# Look two months ahead
precise calendar --days 60
```

**Output Format:**

```
                  Upcoming observations (next 20 days)
┏━━━━━━━━━┳━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Project ┃ Date           ┃ Time (UT)   ┃ Stations                    ┃
┡━━━━━━━━━╇━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ PR405A  │ 3-4 Aug 2026   │ 18:00-04:00 │ Ef, Tr, Ir, Wb, Ur, Mc, JBO │
│ PR406A  │ 9-10 Aug 2026  │ 20:00-05:00 │ Ef, Tr, Ir, Wb, Ur, Mc, JBO │
│ PR407A  │ 11 Aug 2026    │ 18:00-23:00 │ Ef, Tr, Ir, Wb, Ur, Mc, JBO │
│ PR408A  │ 17-18 Aug 2026 │ 18:00-04:00 │ Ef, Tr, Ir, Wb, Ur, Mc, JBO │
└─────────┴────────────────┴─────────────┴─────────────────────────────┘
```

Single-day runs show one date (`11 Aug 2026`); runs crossing midnight show the
start and end days (`3-4 Aug 2026`) with the UT range belonging to those days.

If nothing is scheduled within the window:

```
No observations scheduled in the next 20 days.
```

### daemon

Start the scheduling daemon (polls Google Sheets every N hours).

```bash
precise daemon
```

The daemon runs continuously and:

1. Polls Google Sheets at the interval specified in the config file (`poll_interval_hours`)
2. Picks up each new observation inside the lookahead period (`lookahead_days`),
   once — `state.json` stops a window being scheduled twice
3. Works out which pre-approved FRBs the array can observe (see
   [`plan`](#plan)) and posts that summary to the Slack channel
4. Posts a proposal drawn from that shortlist, and waits for someone to confirm
   it or override the source list
5. Generates the `.key` schedule, posts it to the same Slack channel as the
   prepared observation template to be revised, and commits it as a draft

!!! note "Slack scope"
    Posting the `.key` file needs the `files:write` bot scope. With only
    `chat:write` the upload fails with `missing_scope`; the rest of the
    pipeline is unaffected and the failure is logged, not raised.

Uploading to the antenna server and announcing the final schedule are a
separate, deliberate step — see [`distribute`](#distribute).

**Debug Mode:**

To test the daemon without sending anything to the antenna server:

```bash
precise --debug daemon
```

### plan

Report which FRBs can be observed in the upcoming run, and optionally
write the schedule.

```bash
precise plan [--key] [--epoch 'YYYY-MM-DD HH:MM DURATION'] [--antennas ANTENNA ...]
             [--project PROJECT] [--targets TARGET ...] [--show-dropped]
             [--fringe-finders SPEC ...] [--pulsars SPEC ...]
```

`plan` answers one question: of the FRBs the campaign has approved, which ones
can this array actually observe in this window? It runs in three steps:

1. **Pre-approval** — take the sources `targets.toml` marks as wanted:
   priority 2 always, priority 1 only while the activity database holds a
   detection from the last 30 days. Priority 0 is never considered.

    !!! note
        Until detections are being recorded, no priority-1 source qualifies, so
        `plan` works from the priority-2 sources alone.

2. **Visibility** — hand exactly those sources to planobs, together with the
   observing window and the participating antennas, and ask which stations can
   see each one, sampled every 15 minutes.

3. **The cut** — keep a source only if the whole array, or all but one antenna,
   can observe it at the same moment. Anything patchier is not worth a slot.

It then prints the report: the observation summary, the activity summary,
PRECISE's previous runs, and a table of the observable FRBs with a visibility
grid. This is the same report the daemon posts to Slack, rendered to the
terminal instead, so the two never disagree.

By default nothing is written. Pass `--key` to also generate the `.key`
schedule file for the observable sources — even then nothing is committed,
pushed or posted.

**Optional Arguments:**

- `--epoch 'YYYY-MM-DD HH:MM DURATION'` — Plan this window instead of the next
  calendar entry. `DURATION` is in hours and may be fractional. The calendar is
  not consulted at all, so `--antennas` becomes required.
- `--antennas ANTENNA ...` — Participating antenna codenames. Required with
  `--epoch`; taken from the calendar otherwise.
- `--project PROJECT` — Project code for an `--epoch` run. Defaults to
  `TENTATIVE`. Taken from the calendar otherwise.
- `--targets TARGET ...` — Check only these sources, bypassing pre-approval
  entirely. Accepts internal names (`R200223_D`) or canonical names
  (`FRB20200223B`), in any mix.
- `--show-dropped` — Also list the candidates that too few antennas can
  observe, with the count each one reached.
- `--key` — Also write the `.key` schedule file.
- `--fringe-finders SPEC ...`, `--pulsars SPEC ...` — See *What goes into the
  schedule* below. Only used with `--key`.

**Examples:**

```bash
# What can be observed in the next run?
precise plan

# Check a specific window and array
precise plan --epoch '2026-09-21 14:00 5' --antennas Ef Wb Tr Ir --project PR413A

# Is this particular source up, whatever its priority?
precise plan --targets FRB20121102A

# Show what was cut, and why
precise plan --show-dropped
```

**Output**

```
PR411A: 30 FRB observable

Observation PR411A  from the calendar
07 Sep 2026 19:00 to 05:00 UTC (10.0 h)
8 antennas: Ef, O8, Tr, Ir, Wb, Ur, T6, Mc
planobs checked 36 pre-approved candidate(s); kept those observable by 7 antennas or more.
──────────────────────────────────────────────────────────────────────
Activity summary
Active in the last 15 days: none
Active in the last 30 days only: none
──────────────────────────────────────────────────────────────────────
Previous runs
PR407A   11 Aug 2026  R15_D, R201114_D
PR405A   03 Aug 2026  R4_D, R240209A_D, R14_D

Observable FRBs  (19:00 to 05:00 UTC, 30 min per cell)
🟩 all 8 antennas · 🟨 some · ⬛ none
R65_D        —               9.0h  🟨🟨🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩
R11_D        FRB20190222A    8.5h  🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟨🟨🟨
R240114_D    FRB20240114A    1.0h  🟩🟩🟨🟨🟨🟨🟨🟨🟨🟨🟨🟨🟨🟨🟨🟨🟨⬛⬛⬛
```

Each row gives the internal PRECISE name, the canonical FRB name (`—` when the
source has none), how long the source stays at its best coverage, and a
visibility grid across the run. One cell per half hour, taking the *worst*
coverage in that period — so a green cell means the whole array was on source
for the entire half hour, not merely part of it.

**Previous runs** lists only PRECISE's own projects (`PR…`), and within each
one only the FRB names — the schedules repository also holds unrelated
experiments, and every run is full of calibrators. Non-`PR` files are filtered
out by name before any of them is opened.

The VEX files themselves are read by `precise_scheduler.vex`, which parses
them into a structured document. The repository contains more than one VEX
layout — older PRECISE schedules put `start`, `mode` and `source` on a single
line — and a real parser copes with all of them. Files are parsed across
several worker processes, since the work is CPU-bound in Python and threads
would be no faster; the pool is skipped for small sets and falls back to
serial reading wherever processes cannot be started.

!!! note "Slack"
    The daemon posts this same report to the channel. The table is sent as its
    own message (split further if it is long), because Slack caps how much can
    go in one.

**In daemon mode**

The daemon runs this same stage automatically when an observation approaches,
and posts the resulting summary to the Slack channel before it asks anyone to
confirm a proposal — so the channel sees what is observable at all, and then
what is being proposed from within that list.

### distribute

Compile, upload to vlbeer, and push the final schedule for a project.

```bash
precise distribute --project PROJECT
```

**Required Arguments:**

- `--project PROJECT` — Project code (e.g., `RP036A`)

**Examples:**

```bash
# Finalise and distribute the schedule for project RP036A
precise distribute --project RP036A

# Rehearse it without uploading or notifying anyone
precise --debug distribute --project RP036A
```

The distribute command:

1. Compiles the final schedule for the project
2. Uploads the artefacts to vlbeer via `scp`
3. Commits and pushes the final version to the schedules repository
4. Announces it on Slack and Mattermost

The notification texts are echoed to your terminal so you can see exactly what
was sent:

```
──────────────── Notifications sent ────────────────
Slack: Schedule for RP036A has been uploaded to vlbeer and committed to the schedules repository.
Mattermost: Schedule for RP036A is available on vlbeer. Please download.
```

## Debug Mode

Use the global `--debug` flag to run any command in debug mode. This is useful for testing:

```bash
# Test adding a detection without persisting
precise --debug add-detection --source FRB20201124A --utc "24-11-2020 18:41:02"

# Test the daemon
precise --debug daemon

# Test distribution
precise --debug distribute --project RP036A
```

In debug mode:

- All terminal actions are printed
- Nothing is sent to the antenna server
- Changes are not persisted to external systems
- Useful for testing configuration and workflows
