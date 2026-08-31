# Configuration Reference

PRECISE Scheduler reads its configuration from a single TOML file. This page
describes every key the loader understands.

## Configuration File Location

The scheduler looks for configuration in the following locations, in order, and
uses the first that exists:

1. `$XDG_CONFIG_HOME/precise/config.toml` (only if `XDG_CONFIG_HOME` is set)
2. `~/.config/precise/config.toml`
3. `~/.precise/config.toml`

Override the location with `--config`:

```bash
precise --config /path/to/config.toml daemon
```

The repository ships a working template at `config.toml.example`; copying it is
the fastest way to start:

```bash
mkdir -p ~/.config/precise
cp config.toml.example ~/.config/precise/config.toml
```

## Complete Configuration Template

```toml
[paths]
# Absolute path to the precise_schedules git repository clone on this server.
# targets.toml and activity.csv are expected at the root of this directory.
schedules_repo = "/path/to/precise_schedules"

[google]
# Spreadsheet ID (the long string in the Sheets URL).
# The spreadsheet must be publicly readable ("Anyone with the link can view").
# No credentials file is needed.
spreadsheet_id = "SHEETS-URL-PRECISE-SCHEDULE"

[slack]
# Slack Bot Token (xoxb-...).
bot_token = "xoxb-your-token-here"

# Channel ID (not name) for the scheduling channel (C...).
channel_id = "the-channel-to-push"

# Signing secret used to verify Slack interactive payloads.
signing_secret = "your-signing-secret-here"

# Port the HTTP endpoint listens on for Slack interactive payloads.
# Must be publicly reachable by Slack (configure reverse proxy / tunnel if needed).
webhook_port = 8765

[mattermost]
# Incoming webhook URL for the antenna station Mattermost channel.
webhook_url = "https://mattermost.example.com/hooks/your-hook-id"

[antenna_server]
# SSH connection details for vlbeer.
host = "vlbeer.ira.inaf.it"
user = "vlbeer"
# Absolute path to the SSH private key to use (must be accepted by vlbeer).
ssh_key = "your-ssh-key"
# Base directory on vlbeer under which per-month subdirectories are created.
base_path = "vlbi_arch"

[daemon]
# How often (in hours) the daemon polls the Google Sheets calendar.
poll_interval_hours = 6

# How many days ahead to look for the next observation window.
lookahead_days = 10

[debug]
# Set to true to enable debug mode: no data is sent to the antenna server;
# all antenna/upload interactions are printed to the terminal instead.
enabled = false
```

## Key Reference

### [paths]

| Key | Required | Description |
|-----|----------|-------------|
| `schedules_repo` | yes | Absolute path to the `precise_schedules` git clone. |

Two further paths are **derived** from `schedules_repo` and cannot be set
independently:

- `targets.toml` → `<schedules_repo>/targets.toml`
- the activity database → `<schedules_repo>/activity.csv`

### [google]

| Key | Required | Description |
|-----|----------|-------------|
| `spreadsheet_id` | yes | The long ID from the Sheets URL. |

The spreadsheet must be shared as *Anyone with the link can view* — the
scheduler reads it through the public CSV export endpoint, so **no Google
credentials file is needed**.

Find the ID in the URL:

```
https://docs.google.com/spreadsheets/d/14Pntaz1FvsYyAOzJYjqHk3UpLzJ2gsbBzWrIqMffxYo/edit
                                      ^------------------ spreadsheet_id ----------^
```

### [slack]

| Key | Required | Description |
|-----|----------|-------------|
| `bot_token` | yes | Bot User OAuth Token (`xoxb-…`), from **OAuth & Permissions**. |
| `channel_id` | yes | Channel ID (`C…`), not the channel name. |
| `signing_secret` | yes | From **Basic Information**; verifies interactive payloads. |
| `webhook_port` | no (default `8765`) | Port for the interactive-payload endpoint. |

`webhook_port` must be reachable by Slack for the daemon's confirmation step,
which usually means a reverse proxy or tunnel.

!!! note
    Any Slack user who can see the channel may confirm or override a proposal.
    There is no authorised-users list.

### [mattermost]

| Key | Required | Description |
|-----|----------|-------------|
| `webhook_url` | yes | Incoming webhook URL for the station channel. |

Create it in Mattermost under **Integrations → Incoming Webhooks**.

### [antenna_server]

| Key | Required | Description |
|-----|----------|-------------|
| `host` | yes | Hostname of vlbeer. |
| `user` | yes | SSH username. |
| `ssh_key` | yes | Absolute path to the SSH private key. |
| `base_path` | yes | Base directory for uploads. |

Artefacts are uploaded to `<base_path>/<mmmYY>/`, where `mmmYY` comes from the
observation month — for example `vlbi_arch/Jul26/`.

The key file must have owner-only permissions or SSH will refuse it:

```bash
chmod 600 ~/.ssh/your-ssh-key
```

### [daemon]

| Key | Required | Description |
|-----|----------|-------------|
| `poll_interval_hours` | no (default `6`) | Hours between calendar polls. |
| `lookahead_days` | no (default `10`) | How far ahead to look for the next window. |

### [debug]

| Key | Required | Description |
|-----|----------|-------------|
| `enabled` | no (default `false`) | Enable debug mode globally. |

Debug mode sends nothing to the antenna server, Slack, or Mattermost — every
outbound action is printed to the terminal instead. The `--debug` flag turns it
on for a single run without editing the file:

```bash
precise --debug distribute --project RP036A
```

## Validation

The configuration is validated when any command starts. A missing file, invalid
TOML, or a missing required key each produce a `ConfigError` naming the specific
problem, for example:

```
Missing required config key 'bot_token' in /home/user/.config/precise/config.toml.
Compare your file against config.toml.example.
```

## Secrets Management

`bot_token`, `signing_secret` and `webhook_url` are credentials, and the config
file holds them in plain text. There is no environment-variable override, so
protect the file itself:

```bash
chmod 600 ~/.config/precise/config.toml
```

Never commit a filled-in `config.toml` to version control — commit
`config.toml.example` instead.
