# Activity Database

The activity database is a CSV file that stores all FRB detections recorded by the PRECISE Scheduler. Each row represents a single detection event.

## File Format

The activity database is a standard CSV file with three columns:

| Column | Type | Description |
|--------|------|-------------|
| `canonical_name` | string | Canonical FRB name (e.g., `FRB20201124A`) |
| `utc_datetime` | ISO 8601 string | Detection time in UTC (e.g., `2020-11-24T18:41:02+00:00`) |
| `facility` | string | Detecting facility (e.g., `CHIME`, `Arecibo`, `MeerKAT`) |

## Location

The activity database file is automatically created and maintained at the root
of the schedules repository:

```
<schedules_repo>/activity.csv
```

Where `schedules_repo` is defined in your `config.toml` file:

```toml
[paths]
schedules_repo = "/path/to/precise_schedules"
```

The path is derived, not configurable on its own — `targets.toml` is resolved
the same way, as `<schedules_repo>/targets.toml`.

## File Template

Here's what a complete activity database file looks like:

```csv
canonical_name,utc_datetime,facility
FRB20201124A,2020-11-24T18:41:02+00:00,CHIME
FRB20210315B,2021-03-15T14:22:30+00:00,Arecibo
FRB20210315B,2021-03-15T14:22:32+00:00,CHIME
FRB20220701C,2022-07-01T09:15:45.123456+00:00,MeerKAT
FRB20220701C,2022-07-01T09:15:47.654321+00:00,CHIME
FRB20230108D,2023-01-08T22:30:15+00:00,CHIME
FRB20230425E,2023-04-25T11:45:30.567890+00:00,Arecibo
```

## Features

### Automatic Header Creation

The activity database header row is automatically created when you first add a detection:

```bash
precise add-detection --source FRB20201124A --utc "24-11-2020 18:41:02"
```

### ISO 8601 UTC Timestamps

All timestamps are stored in ISO 8601 format with UTC timezone information:

- **With microseconds**: `2020-11-24T18:41:02.123456+00:00`
- **Without microseconds**: `2020-11-24T18:41:02+00:00`

### Facility Flexibility

The `facility` column can contain any string representing a detecting facility:

- `CHIME` — Canadian Hydrogen Intensity Mapping Experiment
- `Arecibo` — Arecibo Observatory
- `MeerKAT` — MeerKAT radio telescope array
- `FAST` — Five-hundred-meter Aperture Spherical radio Telescope
- Any other facility name

## Data Integrity

The activity database includes several safety features:

### Atomic Writes

File writes are atomic using a temporary file + atomic rename operation, preventing corruption if the process crashes mid-write.

### File Locking

A file-level exclusive lock prevents concurrent corruption when multiple processes access the database simultaneously.

### Manual Backup

While the tool handles file safety, you should:

1. **Version control** the activity database in your schedules repository
2. **Back up regularly** using your standard backup procedures
3. **Monitor for anomalies** using the `precise activity` command

## Querying the Database

The PRECISE Scheduler provides several ways to query the activity database:

### All Recent Detections

```bash
precise activity --since 30d
```

Shows all unique sources with detections in the last 30 days.

### Specific Source

```bash
precise activity --source FRB20201124A --since 30d
```

Shows all detections for a specific source as a formatted table.

### Custom Time Windows

```bash
# Last 7 days
precise activity --since 7d

# Last month
precise activity --since 1month

# Last year
precise activity --since 1yr
```

## Manual Editing

While the CLI provides the recommended way to manage detections, you can manually edit the CSV file:

### If editing manually:

1. **Always keep the header row** (first line): `canonical_name,utc_datetime,facility`
2. **Use ISO 8601 format for timestamps**: `YYYY-MM-DDTHH:MM:SS[.ffffff]+00:00`
3. **Include UTC timezone**: All times must end with `+00:00`
4. **Maintain CSV format**: Each field must be properly comma-separated

### Example of a valid manually-edited file:

```csv
canonical_name,utc_datetime,facility
FRB20201124A,2020-11-24T18:41:02+00:00,CHIME
FRB20210315B,2021-03-15T14:22:30.500000+00:00,Arecibo
```

## Troubleshooting

### "Activity database not found"

This is normal! The database is created automatically when you add your first detection:

```bash
precise add-detection --source FRB20230101X --utc "01-01-2023 12:00:00"
```

### "Permission denied" when accessing database

Ensure the schedules repository and the CSV are writable:

```bash
chmod 755 /path/to/precise_schedules/
chmod 644 /path/to/precise_schedules/activity.csv
```

### "Parsing error" or corrupted data

If the CSV file becomes corrupted:

1. Check the file format with a text editor
2. Verify the header row is intact
3. Restore from your version control history if available
4. Contact the PRECISE team for assistance

## Best Practices

1. **Use the CLI for all modifications** rather than editing manually—it's safer
2. **Keep the database in version control** alongside your schedules repository
3. **Back up regularly** to prevent loss of detection history
4. **Use precise times** including microseconds when available from the detecting facility
5. **Maintain consistent facility names** across your campaign (e.g., always use `CHIME`, not `Chime` or `chime`)
