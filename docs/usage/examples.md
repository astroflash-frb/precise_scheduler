# Usage Examples

Real-world workflows for PRECISE Scheduler.

## Workflow 1: Manual Detection Recording

When a new FRB is detected by CHIME and you want to record it immediately:

```bash
# Record the detection as soon as you hear about it
precise add-detection --source FRB20230615A --utc "15-06-2023 14:32:45.123456"

# Verify it was recorded
precise activity --source FRB20230615A
```

Output:
```
UTC datetime                  Facility
2023-06-15T14:32:45.123456+00:00  CHIME
```

## Workflow 2: Checking Recent Activity

As a scheduler, you want to see all detected FRBs this week to plan follow-up observations:

```bash
# Check all active sources from the last 7 days
precise activity --since 7d
```

Output:
```
Canonical name
FRB20230610B
FRB20230612C
FRB20230615A
```

Then check details for a specific source:

```bash
# Get all detections for FRB20230610B in the last week
precise activity --source FRB20230610B --since 7d
```

## Workflow 3: Detection from Non-CHIME Facility

Record a detection from Arecibo or another facility:

```bash
# Record an Arecibo detection with custom facility flag
precise add-detection --source FRB20230620D --utc "20-06-2023 09:15:30.567890" -f "Arecibo"

# Verify it was recorded with the correct facility
precise activity --source FRB20230620D
```

Output:
```
UTC datetime                  Facility
2023-06-20T09:15:30.567890+00:00  Arecibo
```

## Workflow 4: Correcting a Misrecorded Detection

If you record a detection with the wrong time and need to fix it:

```bash
# Oops! Wrong detection time recorded
precise add-detection --source FRB20230621E --utc "21-06-2023 10:00:00"

# View what we have
precise activity --source FRB20230621E

# Delete the incorrect entry
precise delete-detection --source FRB20230621E --utc "21-06-2023 10:00:00"

# Record the correct detection
precise add-detection --source FRB20230621E --utc "21-06-2023 10:30:45.234567"

# Verify the correction
precise activity --source FRB20230621E
```

## Workflow 5: Monthly Activity Report

Generate a monthly summary of detected FRBs:

```bash
# Check all sources detected in the last month
precise activity --since 1month
```

Then, for each source, check details:

```bash
# Detailed activity for each detected source
for source in $(precise activity --since 1month); do
  echo "=== $source ==="
  precise activity --source "$source" --since 1month
done
```

## Workflow 6: Running the Automatic Daemon

Set up continuous monitoring with the daemon:

```bash
# Start the daemon in the foreground (for testing)
precise daemon

# In production, run as a background service:
# nohup precise daemon &
# or use systemd (see Configuration docs)
```

The daemon automatically:

1. Polls Google Sheets every 6 hours (configurable)
2. Processes new FRB detections
3. Compiles observation schedules
4. Uploads to antenna servers
5. Sends notifications to Slack and Mattermost

## Workflow 7: Scheduling by Hand, Without the Daemon

The daemon posts its proposal to Slack and waits for someone to confirm it.
When you would rather drive that yourself, `precise schedule` runs the same
stages and prints the proposal to your terminal instead:

```bash
# Build a tentative schedule for the next observation in the calendar
precise schedule
```

You get the full proposal — activity summary, previous runs, visibility grid
and the ranked source list — followed by:

```
✓ Scheduled sources: FRB20201124A, FRB20190520B, FRB20200223D
✓ Schedule written to /data/precise_schedules/RP036A.key
Nothing was posted to Slack and nothing was committed. Run 'precise distribute --project RP036A' to finalise.
```

Nothing has been posted or committed, so you can iterate freely.

**Planning a window that is not in the calendar**

Pass `--epoch 'YYYY-MM-DD HH:MM DURATION_HOURS'` to skip the calendar. Because
the calendar is where the antenna list normally comes from, `--antennas`
becomes required:

```bash
precise schedule --epoch '2026-08-01 18:00 8' \
                 --antennas Ef Wb Tr Jb \
                 --project RP036A
```

**Overriding the source list**

If you disagree with the selector, force the sources you want. This bypasses
the priority and activity filters, so you can schedule a priority-0 source or
one that has been quiet for months:

```bash
precise schedule --targets FRB20201124A FRB20200223D
```

The command reports what you overrode:

```
Sources forced via --targets. The selector would have proposed: FRB20201124A, FRB20190520B, FRB20200223D
✓ Scheduled sources: FRB20201124A, FRB20200223D
```

Internal names work too, so `--targets R201124_A R200223_D` is equivalent.

## Workflow 8: Distributing a Project Schedule

Once you are happy with the `.key` file, distribute it:

```bash
# Rehearse it first — nothing is uploaded or announced
precise --debug distribute --project RP036A

# If everything looks good, do it for real
precise distribute --project RP036A
```

Output:
```
✓ Schedule for RP036A distributed.
──────────────── Notifications sent ────────────────
Slack: Schedule for RP036A has been uploaded to vlbeer and committed to the schedules repository.
Mattermost: Schedule for RP036A is available on vlbeer. Please download.
```

Together, `schedule` and `distribute` cover the whole manual path: build and
review a tentative schedule, then compile it, ship it to vlbeer, commit it, and
tell the stations.

## Workflow 9: Multi-Facility Campaign

Tracking FRBs detected across multiple facilities:

```bash
# Record detections from CHIME
precise add-detection --source FRB20230625F --utc "25-06-2023 08:10:20" -f "CHIME"

# Record same source detected by Arecibo (independent detection)
precise add-detection --source FRB20230625F --utc "25-06-2023 08:10:25" -f "Arecibo"

# View all detections of this source
precise activity --source FRB20230625F
```

Output:
```
UTC datetime                  Facility
2023-06-25T08:10:20+00:00     CHIME
2023-06-25T08:10:25+00:00     Arecibo
```

This shows that FRB20230625F was detected independently by both CHIME and Arecibo within 5 seconds, confirming it's a real astrophysical source.

## Workflow 10: Debug Mode Testing

Before rolling out configuration changes, test them safely:

```bash
# Test daemon in debug mode
precise --debug daemon

# Test distribution
precise --debug distribute --project RP036A

# Test with custom config
precise --config /path/to/test/config.toml --debug daemon
```

In debug mode, no data is sent to the antenna server or external systems, making it safe to test new configurations.

## Tips and Best Practices

1. **Always use microseconds in the UTC time** when available from the detecting facility for maximum accuracy:
   ```bash
   precise add-detection --source FRB... --utc "DD-MM-YYYY HH:MM:SS.ffffff"
   ```

2. **Use sensible look-back windows** when checking activity:
   ```bash
   # Last 24 hours
   precise activity --since 1d
   
   # Last week
   precise activity --since 7d
   
   # Last month
   precise activity --since 1month
   ```

3. **Maintain a canonical source naming scheme** (e.g., `FRB20YYMMDDX`) for consistency across the campaign.

4. **Use `--debug` mode liberally** when testing new workflows or configuration changes—it's safe!

5. **Check the activity database regularly** to catch configuration or integration issues early.
