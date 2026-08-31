# PRECISE Scheduler

**PRECISE Scheduler** is an autonomous VLBI scheduling tool for the PRECISE FRB monitoring campaign. It automatically manages observations of Fast Radio Bursts (FRBs) detected by facilities like CHIME, coordinating with VLBI arrays to capture multi-wavelength radio observations.

## Key Features

- **Automated Detection Logging**: Record FRB detections from multiple detecting facilities (CHIME, etc.)
- **Activity Database**: Maintain a searchable history of all detections with source names, detection times, and detecting facilities
- **Observation Scheduling**: Compile and upload observation schedules to antenna servers
- **Daemon Mode**: Run as a background service polling for new detections and automating scheduling decisions
- **Rich CLI**: Colored, user-friendly command-line interface with detailed help text

## What is PRECISE?

PRECISE (Pinpointing Repeating Chime Sources with EVN dishes) is a VLBI monitoring campaign focused on studying Fast Radio Bursts. By coordinating rapid follow-up observations across international VLBI arrays, PRECISE aims to capture the detailed radio morphology and time-variable behavior of FRBs immediately after detection.

## Quick Start

Install the scheduler:

```bash
pip install -e .
```

View the main help:

```bash
precise --help
```

Record a new FRB detection:

```bash
precise add-detection --source FRB20201124A --utc "24-11-2020 18:41:02.00"
```

View recent activity:

```bash
precise activity --since 7d
```

## Next Steps

- [Getting Started](getting-started.md) — Installation and initial setup
- [CLI Reference](usage/cli-commands.md) — Complete command reference
- [Usage Examples](usage/examples.md) — Real-world workflows
- [Activity Database Format](reference/activity-database.md) — Data structure documentation
