# Simulation Monitor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a cron-based PBS simulation monitor that checks job health, sends healthchecks.io pings, and emails notifications via `~/notify.py`.

**Architecture:** A single `check.py` script run by cron every 5 minutes reads a `jobs.json` registry, queries PBS via SSH, evaluates job health (staleness + disappearance), sends per-job and master healthcheck pings, and emails on state changes. A helper `add_job.py` manages the registry.

**Tech Stack:** Python 3 (claude conda env), `requests`, SSH, `~/notify.py`, healthchecks.io API v3, cron

**Spec:** `docs/superpowers/specs/2026-03-21-sim-monitor-design.md`

---

## File Structure

| File | Responsibility |
|---|---|
| `check.py` | Main monitor: .env parsing, jobs.json locking/read/write, qstat querying, staleness detection, healthchecks.io API, notifications, master healthcheck |
| `add_job.py` | CLI to add/remove jobs from `jobs.json` with file locking |
| `jobs.json` | Job registry (JSON array, auto-managed) |
| `.env` | `HEALTHCHECK_API_KEY` and `MASTER_HEALTHCHECK_ID` |

---

### Task 1: .env parsing and healthchecks.io API helpers in `check.py`

**Files:**
- Create: `/home/hyeonu/sim-monitor/check.py`

- [ ] **Step 1: Create `check.py` with .env parsing and healthchecks.io helpers**

```python
#!/usr/bin/env python3
"""Simulation monitor — checks PBS job health and pings healthchecks.io."""

import glob
import json
import fcntl
import logging
import os
import subprocess
import sys
import time
import traceback

import requests

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, ".env")
JOBS_PATH = os.path.join(SCRIPT_DIR, "jobs.json")
LOCK_PATH = os.path.join(SCRIPT_DIR, ".jobs.json.lock")
NOTIFY_SCRIPT = os.path.expanduser("~/notify.py")

HC_API_URL = "https://healthchecks.io/api/v3/checks/"
HC_PING_URL = "https://hc-ping.com/"
HC_TIMEOUT = 10  # seconds

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)


def read_env():
    """Parse .env file, return dict of KEY=VALUE pairs."""
    env = {}
    if not os.path.exists(ENV_PATH):
        return env
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
    return env


def write_env(env):
    """Write dict back to .env file, preserving KEY=VALUE format."""
    with open(ENV_PATH, "w") as f:
        for key, value in env.items():
            f.write(f"{key}={value}\n")


def hc_create_check(api_key, name):
    """Create a new healthcheck via the Management API. Returns the ping URL UUID."""
    resp = requests.post(
        HC_API_URL,
        headers={
            "X-Api-Key": api_key,
            "Content-Type": "application/json",
        },
        json={"name": name, "timeout": 300, "grace": 600},
        timeout=HC_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    # Extract UUID from ping_url: "https://hc-ping.com/<uuid>"
    ping_url = data.get("ping_url", "")
    uuid = ping_url.rstrip("/").split("/")[-1]
    return uuid


def hc_ping(uuid, status="success", body=""):
    """Send a ping to healthchecks.io. status: 'success' or 'fail'."""
    url = HC_PING_URL + uuid
    if status == "fail":
        url += "/fail"
    try:
        if body:
            requests.post(url, timeout=HC_TIMEOUT, data=body.encode("utf-8"))
        else:
            requests.get(url, timeout=HC_TIMEOUT)
    except Exception as e:
        log.error("Failed to ping healthcheck %s: %s", uuid, e)
```

- [ ] **Step 2: Verify syntax**

Run: `/home/hyeonu/anaconda3/envs/claude/bin/python -c "import py_compile; py_compile.compile('/home/hyeonu/sim-monitor/check.py', doraise=True)"`
Expected: No output (success)

- [ ] **Step 3: Commit**

```bash
cd /home/hyeonu/sim-monitor
git add check.py
git commit -m "Add .env parsing and healthchecks.io API helpers"
```

---

### Task 2: File locking and jobs.json read/write

**Files:**
- Modify: `/home/hyeonu/sim-monitor/check.py`

- [ ] **Step 1: Add file locking and jobs.json helpers to `check.py`**

Append after the `hc_ping` function:

```python
def acquire_lock():
    """Acquire exclusive lock on jobs.json. Returns lock file handle."""
    lock_fh = open(LOCK_PATH, "w")
    fcntl.flock(lock_fh, fcntl.LOCK_EX)
    return lock_fh


def release_lock(lock_fh):
    """Release lock on jobs.json."""
    fcntl.flock(lock_fh, fcntl.LOCK_UN)
    lock_fh.close()


def read_jobs():
    """Read jobs.json. Returns empty list if file doesn't exist or is empty."""
    if not os.path.exists(JOBS_PATH):
        return []
    with open(JOBS_PATH) as f:
        content = f.read().strip()
        if not content:
            return []
        return json.loads(content)


def write_jobs(jobs):
    """Write jobs list back to jobs.json."""
    with open(JOBS_PATH, "w") as f:
        json.dump(jobs, f, indent=2)
        f.write("\n")
```

- [ ] **Step 2: Verify syntax**

Run: `/home/hyeonu/anaconda3/envs/claude/bin/python -c "import py_compile; py_compile.compile('/home/hyeonu/sim-monitor/check.py', doraise=True)"`
Expected: No output (success)

- [ ] **Step 3: Commit**

```bash
cd /home/hyeonu/sim-monitor
git add check.py
git commit -m "Add file locking and jobs.json read/write helpers"
```

---

### Task 3: PBS query and staleness detection

**Files:**
- Modify: `/home/hyeonu/sim-monitor/check.py`

- [ ] **Step 1: Add PBS query and staleness functions**

Append after the `write_jobs` function:

```python
class SSHError(Exception):
    """Raised when SSH transport itself fails (not a qstat error)."""
    pass


def query_pbs(job_id):
    """Query PBS job state via SSH.

    Returns (state, info_dict) or (None, {}) if job disappeared.
    Raises SSHError if SSH connection itself fails.
    """
    cmd = ["ssh", "happiness", f"qstat -f {job_id}"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

    if result.returncode != 0:
        # Exit code 255 = SSH transport failure
        # Also check stderr for SSH-specific error messages
        stderr = result.stderr.lower()
        if result.returncode == 255 or "connection refused" in stderr or "no route to host" in stderr or "could not resolve" in stderr:
            raise SSHError(f"SSH failed (exit {result.returncode}): {result.stderr.strip()}")
        # Otherwise, qstat itself failed — job has left the queue
        return None, {}

    info = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        for key in ("Job_Name", "job_state", "exit_status", "exec_host", "resources_used.walltime"):
            if line.startswith(key):
                info[key] = line.split("=", 1)[1].strip()

    state = info.get("job_state")
    return state, info


def check_staleness(output_dir, output_pattern, stale_timeout):
    """Check if output files are stale. Returns (is_stale, latest_mtime_str)."""
    pattern = os.path.join(output_dir, output_pattern)
    matches = glob.glob(pattern)
    if not matches:
        # No output yet — not stale (job may have just started)
        return False, "no output files yet"

    latest_mtime = max(os.path.getmtime(m) for m in matches)
    age_minutes = (time.time() - latest_mtime) / 60.0

    mtime_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(latest_mtime))
    if age_minutes > stale_timeout:
        return True, f"last output at {mtime_str} ({age_minutes:.0f} min ago)"
    return False, f"last output at {mtime_str} ({age_minutes:.0f} min ago)"


def check_success_marker(output_dir):
    """Check if SUCCESS marker file exists."""
    return os.path.exists(os.path.join(output_dir, "SUCCESS"))
```

- [ ] **Step 2: Verify syntax**

Run: `/home/hyeonu/anaconda3/envs/claude/bin/python -c "import py_compile; py_compile.compile('/home/hyeonu/sim-monitor/check.py', doraise=True)"`
Expected: No output (success)

- [ ] **Step 3: Commit**

```bash
cd /home/hyeonu/sim-monitor
git add check.py
git commit -m "Add PBS query and staleness detection"
```

---

### Task 4: Notification helper

**Files:**
- Modify: `/home/hyeonu/sim-monitor/check.py`

- [ ] **Step 1: Add notification function**

Append after `check_success_marker`:

```python
def notify(subject, message):
    """Send email notification via ~/notify.py. Logs errors but does not raise."""
    try:
        result = subprocess.run(
            [sys.executable, NOTIFY_SCRIPT, "-s", subject, "-m", message],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            log.error("notify.py failed (exit %d): %s", result.returncode, result.stderr)
    except Exception as e:
        log.error("Failed to run notify.py: %s", e)
```

- [ ] **Step 2: Verify syntax**

Run: `/home/hyeonu/anaconda3/envs/claude/bin/python -c "import py_compile; py_compile.compile('/home/hyeonu/sim-monitor/check.py', doraise=True)"`
Expected: No output (success)

- [ ] **Step 3: Commit**

```bash
cd /home/hyeonu/sim-monitor
git add check.py
git commit -m "Add notification helper wrapping notify.py"
```

---

### Task 5: Main loop — process each job

**Files:**
- Modify: `/home/hyeonu/sim-monitor/check.py`

- [ ] **Step 1: Add `process_job` function and `main` function**

Append after `notify`:

```python
PASSIVE_STATES = {"Q", "H", "E", "S", "W", "T"}


def process_job(job, api_key):
    """Process a single job. Returns (keep_in_registry, updated_job).

    Raises subprocess.TimeoutExpired or SSHError if qstat SSH fails.
    """
    name = job["name"]
    job_id = job["job_id"]

    # Ensure healthcheck exists
    if not job.get("healthcheck_id"):
        log.info("Creating healthcheck for job %s", name)
        uuid = hc_create_check(api_key, name)
        job["healthcheck_id"] = uuid
        log.info("Created healthcheck %s for job %s", uuid, name)

    hc_id = job["healthcheck_id"]

    # Query PBS
    state, info = query_pbs(job_id)

    if state is None:
        # Job disappeared from qstat
        if check_success_marker(job["output_dir"]):
            log.info("Job %s completed successfully", name)
            hc_ping(hc_id, "success")
            notify(
                f"Job {name} completed",
                f"Job {name} ({job_id}) completed successfully.\n"
                f"Output dir: {job['output_dir']}",
            )
        else:
            log.warning("Job %s disappeared without SUCCESS marker", name)
            hc_ping(hc_id, "fail")
            notify(
                f"Job {name} killed/crashed",
                f"Job {name} ({job_id}) disappeared from qstat without a SUCCESS marker.\n"
                f"Output dir: {job['output_dir']}",
            )
        return False, job  # Remove from registry

    if state in PASSIVE_STATES:
        # Queued or other passive state — job is fine
        log.info("Job %s in state %s, sending success ping", name, state)
        hc_ping(hc_id, "success")
        return True, job

    if state == "R":
        # Running — check staleness
        is_stale, detail = check_staleness(
            job["output_dir"], job["output_pattern"], job["stale_timeout"]
        )
        if is_stale:
            log.warning("Job %s is stale: %s", name, detail)
            hc_ping(hc_id, "fail")
            if not job.get("notified_stale"):
                notify(
                    f"Job {name} stale",
                    f"Job {name} ({job_id}) is running but output is stale.\n"
                    f"{detail}\n"
                    f"Stale timeout: {job['stale_timeout']} minutes\n"
                    f"Output dir: {job['output_dir']}",
                )
                job["notified_stale"] = True
        else:
            log.info("Job %s is running and healthy: %s", name, detail)
            hc_ping(hc_id, "success")
            job["notified_stale"] = False
        return True, job

    # Unknown state — treat as passive
    log.info("Job %s in unknown state %s, sending success ping", name, state)
    hc_ping(hc_id, "success")
    return True, job


def main():
    master_hc_id = None
    try:
        # Read .env
        env = read_env()
        api_key = env.get("HEALTHCHECK_API_KEY")
        if not api_key:
            log.error("HEALTHCHECK_API_KEY not found in .env")
            sys.exit(1)

        # Ensure master healthcheck exists
        master_hc_id = env.get("MASTER_HEALTHCHECK_ID")
        if not master_hc_id:
            log.info("Creating master healthcheck")
            master_hc_id = hc_create_check(api_key, "sim-monitor-master")
            env["MASTER_HEALTHCHECK_ID"] = master_hc_id
            write_env(env)
            log.info("Created master healthcheck: %s", master_hc_id)

        # Read and process jobs
        lock_fh = acquire_lock()
        try:
            jobs = read_jobs()
            if not jobs:
                log.info("No jobs to monitor")
                release_lock(lock_fh)
                hc_ping(master_hc_id, "success")
                return

            ssh_failed = False
            updated_jobs = []
            for job in jobs:
                try:
                    keep, updated_job = process_job(job, api_key)
                    if keep:
                        updated_jobs.append(updated_job)
                except SSHError as e:
                    log.error("SSH failure querying job %s: %s", job["name"], e)
                    ssh_failed = True
                    updated_jobs.append(job)  # Keep job, skip this cycle
                except subprocess.TimeoutExpired:
                    log.error("SSH timeout querying job %s", job["name"])
                    ssh_failed = True
                    updated_jobs.append(job)

            write_jobs(updated_jobs)
        finally:
            release_lock(lock_fh)

        # Master healthcheck ping
        if ssh_failed:
            log.warning("SSH failure detected, sending master failure ping")
            hc_ping(master_hc_id, "fail", "SSH to happiness failed during this cycle")
        else:
            hc_ping(master_hc_id, "success")

    except Exception:
        log.error("Unhandled exception:\n%s", traceback.format_exc())
        if master_hc_id:
            hc_ping(master_hc_id, "fail", traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify syntax**

Run: `/home/hyeonu/anaconda3/envs/claude/bin/python -c "import py_compile; py_compile.compile('/home/hyeonu/sim-monitor/check.py', doraise=True)"`
Expected: No output (success)

- [ ] **Step 3: Commit**

```bash
cd /home/hyeonu/sim-monitor
git add check.py
git commit -m "Add main loop with job processing and master healthcheck"
```

---

### Task 6: `add_job.py` helper CLI

**Files:**
- Create: `/home/hyeonu/sim-monitor/add_job.py`

- [ ] **Step 1: Create `add_job.py`**

```python
#!/usr/bin/env python3
"""Add or remove jobs from the sim-monitor registry."""

import argparse
import fcntl
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
JOBS_PATH = os.path.join(SCRIPT_DIR, "jobs.json")
LOCK_PATH = os.path.join(SCRIPT_DIR, ".jobs.json.lock")


def acquire_lock():
    lock_fh = open(LOCK_PATH, "w")
    fcntl.flock(lock_fh, fcntl.LOCK_EX)
    return lock_fh


def release_lock(lock_fh):
    fcntl.flock(lock_fh, fcntl.LOCK_UN)
    lock_fh.close()


def read_jobs():
    if not os.path.exists(JOBS_PATH):
        return []
    with open(JOBS_PATH) as f:
        content = f.read().strip()
        if not content:
            return []
        return json.loads(content)


def write_jobs(jobs):
    with open(JOBS_PATH, "w") as f:
        json.dump(jobs, f, indent=2)
        f.write("\n")


def add_job(args):
    lock_fh = acquire_lock()
    try:
        jobs = read_jobs()

        # Check for duplicate name
        for j in jobs:
            if j["name"] == args.name:
                print(f"Error: job with name '{args.name}' already exists", file=sys.stderr)
                sys.exit(1)

        job = {
            "job_id": args.job,
            "name": args.name,
            "output_dir": os.path.abspath(args.output_dir),
            "output_pattern": args.output_pattern,
            "stale_timeout": args.stale_timeout,
            "healthcheck_id": None,
            "notified_stale": False,
        }
        jobs.append(job)
        write_jobs(jobs)
        print(f"Added job '{args.name}' ({args.job})")
    finally:
        release_lock(lock_fh)


def remove_job(args):
    lock_fh = acquire_lock()
    try:
        jobs = read_jobs()
        original_len = len(jobs)
        jobs = [j for j in jobs if j["name"] != args.remove]
        if len(jobs) == original_len:
            print(f"Error: no job with name '{args.remove}' found", file=sys.stderr)
            sys.exit(1)
        write_jobs(jobs)
        print(f"Removed job '{args.remove}'")
    finally:
        release_lock(lock_fh)


def list_jobs(args):
    lock_fh = acquire_lock()
    try:
        jobs = read_jobs()
        if not jobs:
            print("No jobs registered.")
            return
        for j in jobs:
            hc = j.get("healthcheck_id") or "(pending)"
            print(f"  {j['name']:20s}  job_id={j['job_id']:20s}  hc={hc}")
    finally:
        release_lock(lock_fh)


def main():
    parser = argparse.ArgumentParser(description="Manage sim-monitor job registry.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="Add a job to monitor")
    p_add.add_argument("--job", required=True, help="PBS job ID (e.g., 12345.happiness)")
    p_add.add_argument("--name", required=True, help="Human-readable job name")
    p_add.add_argument("--output-dir", required=True, help="Path to simulation output directory")
    p_add.add_argument("--output-pattern", required=True, help="Glob pattern for output files (e.g., DD*)")
    p_add.add_argument("--stale-timeout", type=int, required=True, help="Minutes without output before stale")

    p_rm = sub.add_parser("remove", help="Remove a job from monitoring")
    p_rm.add_argument("name", help="Job name to remove")

    sub.add_parser("list", help="List all monitored jobs")

    args = parser.parse_args()

    if args.command == "add":
        add_job(args)
    elif args.command == "remove":
        remove_job(args)
    elif args.command == "list":
        list_jobs(args)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify syntax**

Run: `/home/hyeonu/anaconda3/envs/claude/bin/python -c "import py_compile; py_compile.compile('/home/hyeonu/sim-monitor/add_job.py', doraise=True)"`
Expected: No output (success)

- [ ] **Step 3: Test add and list**

Run:
```bash
cd /home/hyeonu/sim-monitor
/home/hyeonu/anaconda3/envs/claude/bin/python add_job.py add \
  --job 99999.happiness \
  --name test_job \
  --output-dir /tmp \
  --output-pattern "*.txt" \
  --stale-timeout 30
/home/hyeonu/anaconda3/envs/claude/bin/python add_job.py list
```
Expected: "Added job 'test_job'" then list showing the job.

- [ ] **Step 4: Test remove**

Run:
```bash
/home/hyeonu/anaconda3/envs/claude/bin/python add_job.py remove test_job
/home/hyeonu/anaconda3/envs/claude/bin/python add_job.py list
```
Expected: "Removed job 'test_job'" then "No jobs registered."

- [ ] **Step 5: Commit**

```bash
cd /home/hyeonu/sim-monitor
git add add_job.py
git commit -m "Add helper CLI for managing job registry"
```

---

### Task 7: Create `.env` template and initialize empty `jobs.json`

**Files:**
- Create: `/home/hyeonu/sim-monitor/.env.example`
- Create: `/home/hyeonu/sim-monitor/.gitignore`

- [ ] **Step 1: Create `.env.example`**

```
HEALTHCHECK_API_KEY=your-api-key-here
MASTER_HEALTHCHECK_ID=
```

- [ ] **Step 2: Create `.gitignore`**

```
.env
jobs.json
.jobs.json.lock
check.log
```

- [ ] **Step 3: Initialize empty `jobs.json` if it doesn't exist**

Run: `[ -f /home/hyeonu/sim-monitor/jobs.json ] || echo '[]' > /home/hyeonu/sim-monitor/jobs.json`

- [ ] **Step 4: Commit**

```bash
cd /home/hyeonu/sim-monitor
git add .env.example .gitignore
git commit -m "Add .env.example and .gitignore"
```

---

### Task 8: Set up `.env` with real API key and install cron

**Files:**
- Create: `/home/hyeonu/sim-monitor/.env` (from user's real API key)

- [ ] **Step 1: Create `.env` with API key**

Ask user for their healthchecks.io API key and write:
```
HEALTHCHECK_API_KEY=<user-provided-key>
MASTER_HEALTHCHECK_ID=
```

- [ ] **Step 2: Dry-run test of `check.py`**

Run:
```bash
cd /home/hyeonu/sim-monitor
echo '[]' > jobs.json
/home/hyeonu/anaconda3/envs/claude/bin/python check.py
```
Expected: Log output showing "No jobs to monitor" and a successful master healthcheck ping (master healthcheck gets auto-created and written to `.env`).

- [ ] **Step 3: Verify master healthcheck was created**

Run: `cat /home/hyeonu/sim-monitor/.env`
Expected: `MASTER_HEALTHCHECK_ID=<some-uuid>` now populated.

- [ ] **Step 4: Install cron entry**

Run: `crontab -l 2>/dev/null; echo "---adding entry---"`
Then add:
```
(crontab -l 2>/dev/null; echo "*/5 * * * * /home/hyeonu/anaconda3/envs/claude/bin/python /home/hyeonu/sim-monitor/check.py >> /home/hyeonu/sim-monitor/check.log 2>&1") | crontab -
```

- [ ] **Step 5: Verify cron entry**

Run: `crontab -l`
Expected: Entry with `*/5 * * * *` pointing to `check.py`.
