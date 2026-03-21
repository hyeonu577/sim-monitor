# Simulation Monitor — Design Spec

## Overview

A cron-based simulation monitoring tool that watches PBS jobs on the Happiness cluster. It reads a job registry file, checks job health via `qstat` and output file freshness, sends health check pings to healthchecks.io, and notifies the user via email when something goes wrong.

## Goals

- Detect when a simulation job disappears from `qstat` (completed or killed)
- Detect when a running simulation becomes stale (no new output)
- Send periodic health check pings to healthchecks.io (every 5 minutes, 10-minute grace)
- Automatically create new healthchecks via the healthchecks.io Management API
- Notify the user via `~/notify.py` when events occur

## Architecture

Cron runs `check.py` every 5 minutes on Hercules. The script is stateless — all persistent state lives in `jobs.json`. Each cycle reads the registry, checks each job, sends pings/notifications, and writes back any updates.

```
┌─────────┐   cron every 5min   ┌──────────┐
│ Hercules │ ──────────────────> │ check.py │
└─────────┘                     └────┬─────┘
                                     │
                    ┌────────────────┼────────────────┐
                    │                │                 │
                    v                v                 v
            ┌────────────┐  ┌──────────────┐  ┌─────────────┐
            │  jobs.json │  │ ssh happiness │  │healthchecks │
            │  (registry)│  │   qstat -f   │  │  .io API    │
            └────────────┘  └──────────────┘  └─────────────┘
                                     │
                                     v
                              ┌────────────┐
                              │ ~/notify.py│
                              │   (email)  │
                              └────────────┘
```

## Job Registry (`jobs.json`)

A JSON array where each entry represents a monitored simulation:

```json
[
  {
    "job_id": "12345.happiness",
    "name": "phys_run_01",
    "output_dir": "/data1/hyeonu/abyss-comoving-test/collapse-test/physical/run_01/",
    "output_pattern": "DD*",
    "stale_timeout": 60,
    "healthcheck_id": null
  }
]
```

### Fields

| Field | Type | Description |
|---|---|---|
| `job_id` | string | PBS job ID (e.g., `12345.happiness`) |
| `name` | string | Human-readable label, also used as healthcheck name |
| `output_dir` | string | Absolute path to simulation output directory |
| `output_pattern` | string | Glob pattern for output files/dirs (e.g., `DD*`) |
| `stale_timeout` | int | Minutes without new output before considered stale |
| `healthcheck_id` | string or null | UUID from healthchecks.io, auto-populated on first run |

When `healthcheck_id` is `null`, the monitor creates a new healthcheck via the API and writes the UUID back.

## Monitor Logic (`check.py`)

Each cron cycle:

1. Read `.env` for `HEALTHCHECK_API_KEY`
2. Read `jobs.json`
3. For each job:
   - **Healthcheck creation:** If `healthcheck_id` is `null`, create a new check via `POST https://healthchecks.io/api/v3/checks/` with `timeout=300` (5 min), `grace=600` (10 min), and `name` from the job entry. Write the returned UUID back.
   - **Query PBS:** Run `ssh happiness qstat -f <job_id>` to get job state.
   - **Evaluate state** (see state matrix below).
4. Write updated `jobs.json`

### State Matrix

| qstat state | Output fresh? | Action |
|---|---|---|
| Running (R) | Yes | Send success ping to healthchecks.io |
| Running (R) | Stale (mtime > stale_timeout) | Send failure ping + stale notification email |
| Queued (Q) | N/A | Send success ping (waiting is normal) |
| Disappeared | `SUCCESS` marker exists | Send success ping + completed email, remove from `jobs.json` |
| Disappeared | No `SUCCESS` marker | Send failure ping + killed/crashed email, remove from `jobs.json` |

### Staleness Detection

1. Glob `output_dir/output_pattern`
2. Find the most recently modified match (by mtime)
3. If `now - mtime > stale_timeout` minutes, the job is stale

### Healthchecks.io Integration

- **Create check:** `POST https://healthchecks.io/api/v3/checks/` with header `X-Api-Key: <key>`, body `{"name": "<name>", "timeout": 300, "grace": 600}`
- **Success ping:** `GET https://hc-ping.com/<healthcheck_id>`
- **Failure ping:** `GET https://hc-ping.com/<healthcheck_id>/fail`

### Notifications via `~/notify.py`

- **Stale job:** `python ~/notify.py -s "Job <name> stale" -m "<details>"`
- **Job completed:** `python ~/notify.py -s "Job <name> completed" -m "<details>"`
- **Job killed/crashed:** `python ~/notify.py -s "Job <name> killed/crashed" -m "<details>"`

## Helper CLI (`add_job.py`)

Convenience script to add jobs to the registry:

```bash
python ~/sim-monitor/add_job.py \
  --job 12345.happiness \
  --name phys_run_01 \
  --output-dir /data1/hyeonu/.../run_01/ \
  --output-pattern "DD*" \
  --stale-timeout 60
```

Appends a new entry to `jobs.json` with `healthcheck_id: null`. Also supports `--remove <name>` to manually remove a job.

## SUCCESS Marker Convention

PBS job scripts should write a `SUCCESS` file at the end of a successful run:

```bash
# Last line of the simulation run script
touch $OUTPUT_DIR/SUCCESS
```

The monitor checks for `output_dir/SUCCESS` when a job disappears from `qstat`.

## File Layout

```
~/sim-monitor/
├── .env                # HEALTHCHECK_API_KEY=...
├── jobs.json           # Job registry (auto-managed)
├── check.py            # Main monitor script (cron runs this)
├── add_job.py          # Helper to add/remove jobs
└── check.log           # Cron output log
```

## Cron Entry

```
*/5 * * * * /home/hyeonu/anaconda3/envs/claude/bin/python /home/hyeonu/sim-monitor/check.py >> /home/hyeonu/sim-monitor/check.log 2>&1
```

Runs on Hercules every 5 minutes. Uses the `claude` conda environment for dependencies (requests).

## Error Handling

- If `ssh happiness qstat` fails (network issue), skip the job for this cycle and log the error. Do not send failure ping (transient issue, not a job failure).
- If healthchecks.io API call fails, log the error and continue. The missed ping will trigger healthchecks.io's own grace period alerting.
- If `jobs.json` is empty, exit cleanly.
- File locking on `jobs.json` to prevent race conditions between cron and `add_job.py`.

## Dependencies

- Python 3 (claude conda env)
- `requests` library (for healthchecks.io API)
- SSH access to Happiness (already configured)
- `~/notify.py` (already exists)
