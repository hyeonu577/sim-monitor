# sim-monitor

Cron-based PBS simulation monitor with [healthchecks.io](https://healthchecks.io) integration and email notifications.

## What it does

- Checks PBS job status via SSH every 5 minutes
- Detects stale simulations (no new output files within a timeout)
- Detects jobs that disappear from the queue (completed or crashed)
- Sends per-job health check pings to healthchecks.io
- Sends a master health check ping to confirm the monitor itself is running
- Emails notifications on state changes via Gmail SMTP

## Setup

### 1. Healthchecks.io

1. Go to [healthchecks.io](https://healthchecks.io) and create an account
2. Create a new project (or use the default one)
3. Go to **Settings** → **API Access**
4. Create a new API key with **read-write** access
5. Copy the API key for the next step

### 2. Configure `.env`

Copy `.env.example` to `.env` and fill in your values:
```
HEALTHCHECK_API_KEY=your-actual-api-key
MASTER_HEALTHCHECK_ID=
SSH_HOST=your-pbs-host
SMTP_USER=your-gmail@gmail.com
SMTP_PASSWORD=your-gmail-app-password
SMTP_TO=recipient@example.com
```

- `MASTER_HEALTHCHECK_ID` is auto-populated on first run
- For `SMTP_PASSWORD`, use a [Gmail App Password](https://support.google.com/accounts/answer/185833) (not your regular password)

### 3. Install the cron entry

```
*/5 * * * * /path/to/python /path/to/sim-monitor/check.py >> /path/to/sim-monitor/check.log 2>&1
```

### 4. Prepare your simulations

Add a **conditional** SUCCESS marker at the end of your simulation run scripts so that
`touch SUCCESS` only runs when the simulation exits cleanly:

```bash
set -o pipefail   # ensure pipe returns mpirun's exit code, not awk's

mpirun -np 36 ./enzo.exe MyParams.enzo 2>&1 \
  | awk '{ print strftime("[%Y-%m-%d %H:%M:%S]"), $0; fflush() }' > run_log

if [ $? -eq 0 ]; then
    touch $PBS_O_WORKDIR/SUCCESS
fi
```

**Why `set -o pipefail`?** Without it, `$?` reflects the exit code of `awk` (the last
command in the pipe), which is almost always 0 even if `mpirun` crashed.

## Usage

### Add a job to monitor

```bash
python /path/to/sim-monitor/manage_jobs.py add \
  --job 12345.pbs-host \
  --name my_sim \
  --output-dir /path/to/project/run_01/ \
  --output-pattern "DD*" \
  --stale-timeout 60 \  # minutes
  --channels "*" \
  --restart-snapshot DD0050  # optional: exclude snapshots up to and including this one
```

`--channels` specifies which healthchecks.io notification integrations to attach (default: `"*"` for all). Use comma-separated channel UUIDs or names, or `"*"` for all configured integrations.

`--restart-snapshot` (optional) specifies the restart snapshot name. All snapshots up to and including this one are excluded from staleness checks, since they originate from a previous run and have old modification times.

### List available notification channels

```bash
python /path/to/sim-monitor/manage_jobs.py list-channels
```

### List monitored jobs

```bash
python /path/to/sim-monitor/manage_jobs.py list
```

### Remove a job

```bash
python /path/to/sim-monitor/manage_jobs.py remove my_sim
```

This removes the job from the registry, deletes the PBS job via `qdel`, and deletes its healthcheck.

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
- `python-dotenv`
- SSH access to PBS host (configured via `SSH_HOST` in `.env`)
