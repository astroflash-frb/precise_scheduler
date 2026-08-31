# PRECISE Scheduler

Observation scheduler assistant for the **PRECISE** VLBI project (*Pinpointing REpeating ChIme Sources with EVN dishes*), which monitors repeating Fast Radio Bursts with the EVN antennas.

It keeps a database of FRB detections, reads the PRECISE observing calendar, works out which sources the array can actually see in the next run, writes the `.key` schedule file, and distributes the compiled schedule to the antennas — either interactively from the command line or unattended as a daemon.


## What it does

- **Activity database** — a plain `activity.csv` of FRB detections (source, UTC, detecting facility), fed manually or automatically from CHIME VOEvents. (_to be done_)
- **Calendar** — reads the PRECISE Google Sheets calendar to find the upcoming observing runs, their time windows and participating stations.
- **Planning** — combines the target list (`targets.toml`), recent activity and `PlanObs` visibility to propose the FRBs observable by the whole array (or all but one antenna) during the run, and optionally writes the `.key` schedule.
- **Distribution** — compiles the schedule with `sched.py`, uploads the products to `vlbeer`, commits the final version, and announces it on Slack and Mattermost.
- **Daemon** — polls the calendar and runs the whole chain automatically, posting the proposal to Slack for a human to confirm before anything is written.


## Installation

Requires Python ≥ 3.12.

```bash
git clone https://github.com/jive-vlbi/precise-scheduler.git
cd precise-scheduler
pip install .          # or: pip install -e ".[dev]" for tests and docs
```

Two external tools are used:

- [`vlbiplanobs`](https://github.com/bmarcote/vlbi_calculator) (installed as dependency) — provides the `planobs` machinery used for visibility and `.key` generation.
- `sched.py` (NRAO SCHED) (needs to be installed by the users and visible on `PATH`) — used by `precise distribute` to compile the schedule (_to be done_).


## Configuration

Copy `config.toml.example` and fill it in. The first of these paths that exists is used:

1. `$XDG_CONFIG_HOME/precise/config.toml`
2. `~/.config/precise/config.toml`
3. `~/.precise/config.toml`

```bash
mkdir -p ~/.config/precise
cp config.toml.example ~/.config/precise/config.toml
```

It holds the path to the `precise_schedules` GitHub repository (where `targets.toml` and `activity.csv` live), the Google Sheets ID, the Slack and Mattermost credentials, and the `vlbeer` SSH details. Any command accepts `--config PATH` to use a different file.


## Commands

All commands are subcommands of `precise`. Run `precise <command> --help` for the full argument list.

| Command | Description |
| --- | --- |
| `activity` | Show FRB detections from the activity database. |
| `add-detection` | Record a new FRB detection. |
| `delete-detection` | Remove a detection record. |
| `calendar` | List the upcoming observing runs and whether each is scheduled and uploaded. |
| `plan` | Report which FRBs can be observed in the upcoming run, optionally writing the `.key` file. |
| `distribute` | Compile, upload to `vlbeer` and announce the final schedule for a project. |
| `daemon` | Run the scheduler unattended, polling the calendar and writing messages to Slack. |

Global options: `--config PATH` (alternative config file) and `--debug` (print every antenna/upload action instead of sending anything). And `-h` for help in any option.


### `precise activity`

Detections in a look-back window (`--since`, default `30d`; accepts `15d`, `2month`, `1yr`). Without `--source` it lists the sources that were active.

```bash
precise activity --since 15d
precise activity --source FRB20201124A --since 1yr
```


### `precise add-detection` / `precise delete-detection`

Add or remove a record. Times are UTC, `'DD-MM-YYYY HH:MM[:SS.ffffff]'`. `--facility` defaults to `CHIME`.

```bash
precise add-detection --source FRB20201124A --utc "24-11-2020 18:41:02.00"
precise delete-detection --source FRB20201124A --utc "24-11-2020 18:41:02.00"
```


### `precise calendar`

The upcoming runs from the Google Sheets calendar: project, dates, UT range and stations, plus a tick for whether a `.key` exists in the schedules repository and whether it is already on `vlbeer`.

```bash
precise calendar --days 30
precise calendar --no-vlbeer      # skip the ssh check
```


### `precise plan`

Takes the next observing window and the pre-approved sources (priority 2 always; priority 1 only while recently active), asks `planobs` what the array can see, and prints the same report the daemon posts to Slack: observation summary, recent activity, previous PRECISE runs, and the observable FRBs with their visibility grid.

```bash
precise plan                                    # next run in the calendar
precise plan --show-dropped                     # also list the near-misses
precise plan --epoch "2026-08-31 18:00 10" --antennas Ef Wb Tr Ir
precise plan --targets FRB20200223B             # check specific sources only
precise plan --key --pulsars 2                  # also write the .key file
```

Without `--key` nothing is written; nothing is ever committed, pushed or posted by this command.


### `precise distribute`

Finalises a project: compiles the schedule, uploads the products to `vlbeer`, commits the final version, and posts the announcements (echoed to the terminal).

```bash
precise distribute --project RP036A
```


### `precise daemon`

Polls the calendar every `poll_interval_hours` and, when a run falls within `lookahead_days`, runs the pipeline once: proposes sources on Slack, waits for a human confirmation (_to be done_), then generates and commits the draft schedule.

```bash
precise daemon
precise daemon --debug     # nothing is sent to the antenna server
```



## Typical workflow

```bash
precise calendar                      # what is coming up
precise activity --since 30d          # which FRBs have been bursting
precise plan                          # what the array can observe
precise plan --key                    # write the .key schedule
precise distribute --project RP036A   # compile, upload and announce
```


## Development

```bash
pip install -e ".[dev]"
pytest                                # -m "not integration" to skip live services
mkdocs serve                          # documentation at http://127.0.0.1:8000
```


## Documentation

Full documentation lives in `docs/`: [Getting Started](docs/getting-started.md), [CLI Reference](docs/usage/cli-commands.md), [Examples](docs/usage/examples.md), [Configuration](docs/reference/configuration.md) and the [Activity Database format](docs/reference/activity-database.md).


## License

GNU General Public License v3.0 — see [LICENSE](LICENSE).
