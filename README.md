# sim-monitor

Cron-based PBS simulation monitor with [healthchecks.io](https://healthchecks.io) integration and email notifications.

## What it does

- Checks PBS job status via SSH every 5 minutes
- Detects stale simulations (no new output files within a timeout)
- Detects jobs that disappear from the queue (completed or crashed)
- Sends per-job health check pings to healthchecks.io
- Sends a master health check ping to confirm the monitor itself is running
- Emails notifications via `~/notify.py` on state changes

## Setup

1. Copy `.env.example` to `.env` and fill in your values:
   ```
   HEALTHCHECK_API_KEY=your-actual-key
   MASTER_HEALTHCHECK_ID=
   SSH_HOST=your-pbs-host
   ```
   `MASTER_HEALTHCHECK_ID` is auto-populated on first run.

2. Install the cron entry:
   ```
   */5 * * * * /path/to/python /path/to/sim-monitor/check.py >> /path/to/sim-monitor/check.log 2>&1
   ```

3. Add `touch $OUTPUT_DIR/SUCCESS` as the last line of your simulation run scripts.

## Usage

### Add a job to monitor

```bash
python ~/sim-monitor/add_job.py add \
  --job 12345.pbs-host \
  --name my_sim \
  --output-dir /path/to/project/run_01/ \
  --output-pattern "DD*" \
  --stale-timeout 60 \
  --channels "*"
```

`--channels` specifies which healthchecks.io notification integrations to attach (default: `"*"` for all). Use comma-separated channel UUIDs or names, or `"*"` for all configured integrations.

### List available notification channels

```bash
python ~/sim-monitor/add_job.py list-channels
```

### List monitored jobs

```bash
python ~/sim-monitor/add_job.py list
```

### Remove a job

```bash
python ~/sim-monitor/add_job.py remove my_sim
```

## How it works

Each cron cycle, `check.py`:

1. Reads `.env` for API key and master healthcheck ID
2. Creates the master healthcheck if it doesn't exist yet
3. For each job in `jobs.json`:
   - Creates a per-job healthcheck if needed
   - Queries `qstat -f` via SSH to `SSH_HOST`
   - Evaluates job state:

| State | Output | Action |
|---|---|---|
| Running | Fresh | Success ping |
| Running | Stale | Failure ping + email (once) |
| Queued | N/A | Success ping |
| Disappeared | SUCCESS marker | Success ping + completed email, remove from registry |
| Disappeared | No marker | Failure ping + crashed email, remove from registry |

4. Pings the master healthcheck (success if clean cycle, failure if SSH or other error)

## Dependencies

- Python 3
- `requests`
- SSH access to PBS host (configured via `SSH_HOST` in `.env`)
- `~/notify.py`
