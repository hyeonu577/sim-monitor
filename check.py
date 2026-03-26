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
from dotenv import load_dotenv, set_key

from mailer import send_email

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, ".env")
JOBS_PATH = os.path.join(SCRIPT_DIR, "jobs.json")
LOCK_PATH = os.path.join(SCRIPT_DIR, ".jobs.json.lock")

load_dotenv(ENV_PATH)

HC_API_URL = "https://healthchecks.io/api/v3/checks/"
HC_PING_URL = "https://hc-ping.com/"
HC_TIMEOUT = 10  # seconds

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)


def hc_create_check(api_key, name, channels="*"):
    """Create a new healthcheck via the Management API. Returns the ping URL UUID."""
    resp = requests.post(
        HC_API_URL,
        headers={
            "X-Api-Key": api_key,
            "Content-Type": "application/json",
        },
        json={"name": name, "timeout": 300, "grace": 720, "channels": channels},
        timeout=HC_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    # Extract UUID from ping_url: "https://hc-ping.com/<uuid>"
    ping_url = data.get("ping_url", "")
    uuid = ping_url.rstrip("/").split("/")[-1]
    if not uuid:
        raise ValueError(f"Failed to extract UUID from healthchecks.io response: {data}")
    return uuid


def hc_delete(api_key, uuid):
    """Delete a healthcheck from healthchecks.io."""
    try:
        resp = requests.delete(
            HC_API_URL + uuid,
            headers={"X-Api-Key": api_key},
            timeout=HC_TIMEOUT,
        )
        resp.raise_for_status()
        log.info("Deleted healthcheck %s", uuid)
    except Exception as e:
        log.error("Failed to delete healthcheck %s: %s", uuid, e)


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


class SSHError(Exception):
    """Raised when SSH transport itself fails (not a qstat error)."""
    pass


def query_pbs(job_id, ssh_host):
    """Query PBS job state via SSH.

    Returns (state, info_dict) or (None, {}) if job disappeared.
    Raises SSHError if SSH connection itself fails.
    """
    cmd = ["ssh", ssh_host, f"qstat -f {job_id}"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

    if result.returncode != 0:
        # Exit code 255 = SSH transport failure
        # Also check stderr for SSH-specific error messages
        stderr = result.stderr.lower()
        if result.returncode == 255 or "connection refused" in stderr or "no route to host" in stderr or "could not resolve" in stderr or "network is unreachable" in stderr or "permission denied" in stderr or "host key verification failed" in stderr:
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


def check_staleness(output_dir, output_pattern, stale_timeout, restart_snapshot=None):
    """Check if output files are stale. Returns (is_stale, latest_mtime_str)."""
    pattern = os.path.join(output_dir, output_pattern)
    matches = glob.glob(pattern)
    if restart_snapshot:
        matches = [m for m in matches if os.path.basename(m) > restart_snapshot]
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


def notify(subject, message, smtp_user, smtp_password, smtp_to):
    """Send email notification. Logs errors but does not raise."""
    if not all([smtp_user, smtp_password, smtp_to]):
        log.warning("SMTP not configured, skipping email notification")
        return
    subject = f"[Hercules Noti] {subject}"
    if not send_email(subject, message, smtp_user, smtp_password, smtp_to):
        log.error("Email notification failed: %s", subject)


PASSIVE_STATES = {"Q", "H", "E", "S", "W", "T"}


def process_job(job, api_key, ssh_host, smtp_cfg):
    """Process a single job. Returns (keep_in_registry, updated_job, disappearance_info).

    disappearance_info is None when keep_in_registry is True.
    When keep_in_registry is False, disappearance_info is a dict with keys:
      'success' (bool), 'hc_id' (str), 'subject' (str), 'message' (str).
    Pings and emails for disappeared jobs are NOT sent here; the caller must
    write jobs.json first, then use disappearance_info to notify.

    Raises SSHError if SSH connection fails.
    Raises subprocess.TimeoutExpired if SSH/qstat times out.
    """
    name = job["name"]
    job_id = job["job_id"]

    # Ensure healthcheck exists
    if not job.get("healthcheck_id"):
        log.info("Creating healthcheck for job %s", name)
        uuid = hc_create_check(api_key, name, job.get("channels", "*"))
        job["healthcheck_id"] = uuid
        log.info("Created healthcheck %s for job %s", uuid, name)

    hc_id = job["healthcheck_id"]

    # Query PBS
    state, info = query_pbs(job_id, ssh_host)

    if state is None:
        # Job disappeared from qstat — return details without notifying yet.
        # The caller must write jobs.json BEFORE sending pings/emails.
        if check_success_marker(job["output_dir"]):
            log.info("Job %s completed successfully", name)
            disappearance_info = {
                "success": True,
                "hc_id": hc_id,
                "subject": f"Job {name} completed",
                "message": (
                    f"Job {name} ({job_id}) completed successfully.\n"
                    f"Output dir: {job['output_dir']}"
                ),
            }
        else:
            log.warning("Job %s disappeared without SUCCESS marker", name)
            disappearance_info = {
                "success": False,
                "hc_id": hc_id,
                "subject": f"Job {name} killed/crashed",
                "message": (
                    f"Job {name} ({job_id}) disappeared from qstat without a SUCCESS marker.\n"
                    f"Output dir: {job['output_dir']}"
                ),
            }
        return False, job, disappearance_info  # Remove from registry

    if state in PASSIVE_STATES:
        # Queued or other passive state — job is fine
        log.info("Job %s in state %s, sending success ping", name, state)
        hc_ping(hc_id, "success")
        return True, job, None

    if state == "R":
        # Running — check staleness
        is_stale, detail = check_staleness(
            job["output_dir"], job["output_pattern"], job["stale_timeout"],
            restart_snapshot=job.get("restart_snapshot"),
        )
        if is_stale:
            log.warning("Job %s is stale: %s", name, detail)
            stale_reason = (
                f"Job {name} ({job_id}) is running but output is stale.\n"
                f"{detail}\n"
                f"Stale timeout: {job['stale_timeout']} minutes\n"
                f"Output dir: {job['output_dir']}"
            )
            hc_ping(hc_id, "fail", stale_reason)
            if not job.get("notified_stale"):
                notify(f"Job {name} stale", stale_reason, **smtp_cfg)
                job["notified_stale"] = True
        else:
            log.info("Job %s is running and healthy: %s", name, detail)
            hc_ping(hc_id, "success")
            job["notified_stale"] = False
        return True, job, None

    # Unknown state — treat as passive
    log.info("Job %s in unknown state %s, sending success ping", name, state)
    hc_ping(hc_id, "success")
    return True, job, None


def main():
    master_hc_id = None
    try:
        api_key = os.environ.get("HEALTHCHECK_API_KEY")
        if not api_key:
            log.error("HEALTHCHECK_API_KEY not found in .env")
            sys.exit(1)

        ssh_host = os.environ.get("SSH_HOST")
        if not ssh_host:
            log.error("SSH_HOST not found in .env")
            sys.exit(1)

        smtp_cfg = {
            "smtp_user": os.environ.get("SMTP_USER", ""),
            "smtp_password": os.environ.get("SMTP_PASSWORD", ""),
            "smtp_to": os.environ.get("SMTP_TO", ""),
        }

        # Ensure master healthcheck exists
        master_hc_id = os.environ.get("MASTER_HEALTHCHECK_ID")
        if not master_hc_id:
            log.info("Creating master healthcheck")
            master_hc_id = hc_create_check(api_key, "sim-monitor-master")
            set_key(ENV_PATH, "MASTER_HEALTHCHECK_ID", master_hc_id)
            os.environ["MASTER_HEALTHCHECK_ID"] = master_hc_id
            log.info("Created master healthcheck: %s", master_hc_id)

        # Read and process jobs
        ssh_failed = False
        pending_notifications = []
        lock_fh = acquire_lock()
        try:
            jobs = read_jobs()
            if jobs:
                updated_jobs = []
                for job in jobs:
                    try:
                        keep, updated_job, disappearance_info = process_job(job, api_key, ssh_host, smtp_cfg)
                        if keep:
                            updated_jobs.append(updated_job)
                        else:
                            pending_notifications.append(disappearance_info)
                    except SSHError as e:
                        log.error("SSH failure querying job %s: %s", job["name"], e)
                        ssh_failed = True
                        updated_jobs.append(job)
                    except subprocess.TimeoutExpired:
                        log.error("SSH timeout querying job %s", job["name"])
                        ssh_failed = True
                        updated_jobs.append(job)

                write_jobs(updated_jobs)
            else:
                log.info("No jobs to monitor")
        finally:
            release_lock(lock_fh)

        # Send pings/emails for disappeared jobs (jobs.json already updated above)
        for info in pending_notifications:
            status = "success" if info["success"] else "fail"
            hc_ping(info["hc_id"], status, info["message"])
            notify(info["subject"], info["message"], **smtp_cfg)
            hc_delete(api_key, info["hc_id"])

        # Master healthcheck ping
        if ssh_failed:
            log.warning("SSH failure detected, sending master failure ping")
            hc_ping(master_hc_id, "fail", f"SSH to {ssh_host} failed during this cycle")
        else:
            hc_ping(master_hc_id, "success")

    except Exception:
        log.error("Unhandled exception:\n%s", traceback.format_exc())
        if master_hc_id:
            hc_ping(master_hc_id, "fail", traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
