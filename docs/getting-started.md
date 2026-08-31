# Getting Started

## Installation

### Prerequisites

- Python 3.11 or higher
- pip or uv package manager

### Basic Installation

Clone the repository and install in editable mode:

```bash
git clone https://github.com/jive-vlbi/precise-scheduler.git
cd precise-scheduler
pip install -e .
```

Or with `uv`:

```bash
uv pip install -e .
```

### Development Installation

For development with testing and documentation tools:

```bash
pip install -e ".[dev]"
```

This includes pytest, mkdocs, and other development dependencies.

## Configuration

PRECISE Scheduler requires a configuration file. The scheduler looks for configuration in the following locations (in order of preference):

1. `$XDG_CONFIG_HOME/precise/config.toml`
2. `~/.config/precise/config.toml`
3. `~/.precise/config.toml`

### Configuration File Format

Create a `config.toml` file with the following sections:

```toml
[paths]
schedules_repo = "/path/to/schedules/repository"

[google]
google_spreadsheet_id = "your-spreadsheet-id"

[slack]
slack_bot_token = "xoxb-your-token"
slack_channel_id = "C1234567890"
slack_signing_secret = "your-signing-secret"
slack_webhook_port = 3000

[mattermost]
mattermost_webhook_url = "https://mattermost.example.com/hooks/..."

[antenna_server]
antenna_host = "antenna.example.com"
antenna_user = "schedulerbot"
antenna_ssh_key = "/path/to/ssh/key"
antenna_base_path = "/schedules"

[daemon]
poll_interval_hours = 6
lookahead_days = 10
```

### Environment-Specific Setup

For production deployments, consider:

1. **Secrets Management**: Store sensitive values (tokens, SSH keys) using environment variables or a secrets manager
2. **Activity Database**: Ensure the activity database directory is writable and backed up
3. **Log Rotation**: Configure logging to prevent log files from growing unbounded
4. **Systemd Service** (Optional): Create a systemd unit to run the daemon automatically

## Verification

Verify your installation:

```bash
precise --help
```

You should see colored output listing all available commands.

## Troubleshooting

### "Config file not found"

Ensure your `config.toml` exists in one of the expected locations. You can override the default path:

```bash
precise --config /path/to/config.toml activity
```

### "Permission denied" on antenna_ssh_key

Ensure the SSH key has appropriate permissions (typically `600`):

```bash
chmod 600 /path/to/ssh/key
```

### "Can't connect to Google Sheets"

Verify that:

1. The Google Sheets API is enabled in your GCP project
2. Your Google credentials are properly configured
3. The spreadsheet ID in the config is correct
