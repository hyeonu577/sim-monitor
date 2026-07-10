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
    """Delete a healthcheck from healthchecks.io.

    Returns True on success, False on failure (logged, never raises).
    """
    try:
        resp = requests.delete(
            HC_API_URL + uuid,
            headers={"X-Api-Key": api_key},
            timeout=HC_TIMEOUT,
        )
        resp.raise_for_status()
        log.info("Deleted healthcheck %s", uuid)
        return True
    except Exception as e:
        log.error("Failed to delete healthcheck %s: %s", uuid, e)
        return False


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


class TransientError(Exception):
    """Raised when the cluster can't be queried this cycle, so the job's real
    state is unknown (SSH transport failure OR a pbs_server communication
    failure). The job must NOT be declared dead — the caller keeps it in the
    registry and retries next cycle."""
    pass


class SSHError(TransientError):
    """Transient failure specifically in the SSH transport layer."""
    pass


def query_pbs(job_id, ssh_host):
    """Query PBS job state via SSH.

    Returns (state, info_dict), or (None, {}) when qstat explicitly reports the
    job as no longer in the queue — either an unknown job id, or a finished job
    that has moved to PBS history (plain `qstat -f` exits 35, "Job has finished").
    Raises SSHError if the SSH transport itself fails, or TransientError if the
    query fails for any other reason (e.g. pbs_server temporarily unreachable) —
    in both cases the job's real state is unknown and it must not be declared dead.
    Raises subprocess.TimeoutExpired if the SSH/qstat call exceeds its timeout.
    """
    cmd = ["ssh", ssh_host, f"qstat -f {job_id}"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

    if result.returncode != 0:
        stderr = result.stderr.lower()
        # Genuine "job left the queue", two definitive PBS signals:
        #   1. pbs_server reports the id unknown — the job is fully gone
        #      (PBS Pro/Torque: exit 153, stderr "qstat: Unknown Job Id <id>").
        #   2. the job finished and moved to history — plain `qstat -f <id>`
        #      (no -x) then exits 35 with "Job has finished, use -x or -H".
        # Both mean the job is no longer active in the queue, so they take
        # precedence over the transport heuristics below (which key off generic
        # substrings like "permission denied" that could otherwise collide with
        # a real qstat message).
        if (result.returncode in (35, 153)
                or "unknown job id" in stderr
                or "job has finished" in stderr):
            return None, {}
        # SSH transport failure — ssh itself couldn't connect/authenticate.
        if result.returncode == 255 or "connection refused" in stderr or "no route to host" in stderr or "could not resolve" in stderr or "network is unreachable" in stderr or "permission denied" in stderr or "host key verification failed" in stderr:
            raise SSHError(f"SSH transport failed (exit {result.returncode}): {result.stderr.strip()}")
        # SSH succeeded but qstat failed for another reason (pbs_server
        # overloaded/restarting, premature EOF, ...). The job's state is unknown,
        # so do NOT declare it dead — treat as transient and retry next cycle.
        raise TransientError(
            f"qstat failed (exit {result.returncode}), job state unknown: "
            f"{result.stderr.strip() or result.stdout.strip() or '(no output)'}"
        )

    info = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        for key in ("Job_Name", "job_state", "exit_status", "exec_host", "resources_used.walltime"):
            if line.startswith(key):
                info[key] = line.split("=", 1)[1].strip()

    state = info.get("job_state")
    if state is None:
        # qstat exited 0 but no job_state was found (empty or malformed output,
        # e.g. a truncated/racy response). The job's real state is unknown, so do
        # NOT let the caller read this as the job being gone — treat as transient.
        raise TransientError(
            f"qstat exited 0 but no job_state parsed for {job_id}; "
            f"output: {result.stdout.strip()[:200] or '(empty)'}"
        )
    return state, info


def check_staleness(output_dir, output_pattern, stale_timeout, restart_snapshot=None):
    """Check if output files are stale.

    Returns (is_stale, detail, latest_name) where latest_name is the basename of
    the newest matching file/dir, or None if there are no matches yet.
    """
    pattern = os.path.join(output_dir, output_pattern)
    matches = glob.glob(pattern)
    if restart_snapshot:
        matches = [m for m in matches if os.path.basename(m) > restart_snapshot]
    if not matches:
        # No output yet — not stale (job may have just started)
        return False, "no output files yet", None

    latest_match = max(matches, key=os.path.getmtime)
    latest_mtime = os.path.getmtime(latest_match)
    latest_name = os.path.basename(latest_match)
    age_minutes = (time.time() - latest_mtime) / 60.0

    mtime_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(latest_mtime))
    detail = f"last output at {mtime_str} ({age_minutes:.0f} min ago)"
    return age_minutes > stale_timeout, detail, latest_name


def check_success_marker(work_dir):
    """Check if SUCCESS marker file exists in the given directory."""
    return os.path.exists(os.path.join(work_dir, "SUCCESS"))


def notify(subject, message, smtp_user, smtp_password, smtp_to):
    """Send email notification. Logs errors but does not raise.

    Returns True when the email went out (or SMTP is deliberately
    unconfigured — a skip, not an error), False when the send failed.
    """
    if not all([smtp_user, smtp_password, smtp_to]):
        log.warning("SMTP not configured, skipping email notification")
        return True
    if not send_email(subject, message, smtp_user, smtp_password, smtp_to):
        log.error("Email notification failed: %s", subject)
        return False
    return True


def send_notifications(pending_notifications, api_key, smtp_cfg):
    """Ping, email, and delete the healthcheck for each disappeared job.

    Returns a list of error strings for failures that must flip the master
    check to fail — these notifications are one-shot (the job is already out
    of the registry), so a silent failure would lose the alert forever.
    The check is deleted even when the email fails; otherwise it lingers as
    an orphan and its missed pings raise a second, misleading alert.
    """
    errors = []
    for info in pending_notifications:
        status = "success" if info["success"] else "fail"
        hc_ping(info["hc_id"], status, info["message"])
        if not notify(info["subject"], info["message"], **smtp_cfg):
            errors.append(f"email notification failed: {info['subject']}")
        if not hc_delete(api_key, info["hc_id"]):
            errors.append(f"failed to delete healthcheck {info['hc_id']} ({info['subject']})")
    return errors


def send_master_ping(master_hc_id, query_failed, errors):
    """End-of-cycle master ping policy.

    - errors occurred → fail ping carrying the details (takes precedence over
      a transient query failure in the same cycle);
    - transient-only cycle → no ping at all: a one-off blip stays silent, and
      a persistent outage runs past the check's grace period so healthchecks.io
      flags the missed pings on its own;
    - clean cycle → success ping.
    """
    if errors:
        log.warning("%d error(s) this cycle, sending master failure ping", len(errors))
        hc_ping(master_hc_id, "fail", "\n".join(errors))
    elif query_failed:
        log.warning("Transient cluster query failure, skipping master ping this cycle")
    else:
        hc_ping(master_hc_id, "success")


PASSIVE_STATES = {"Q", "H", "E", "S", "W", "T"}


def process_job(job, api_key, ssh_host, smtp_cfg):
    """Process a single job. Returns (keep_in_registry, updated_job, disappearance_info).

    disappearance_info is None when keep_in_registry is True.
    When keep_in_registry is False, disappearance_info is a dict with keys:
      'success' (bool), 'hc_id' (str), 'subject' (str), 'message' (str).
    Pings and emails for disappeared jobs are NOT sent here; the caller must
    write jobs.json first, then use disappearance_info to notify.

    Raises TransientError (incl. its SSHError subclass) if the cluster can't be
    queried this cycle, or subprocess.TimeoutExpired if SSH/qstat times out.
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
        if check_success_marker(job.get("work_dir") or job["output_dir"]):
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
        hc_ping(hc_id, "success", f"Job in state {state}")
        return True, job, None

    if state == "R":
        # Running — check staleness
        is_stale, detail, latest_name = check_staleness(
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
        else:
            log.info("Job %s is running and healthy: %s", name, detail)
            if latest_name:
                body = f"Running — latest snapshot: {latest_name} ({detail})"
            else:
                body = f"Running — {detail}"
            hc_ping(hc_id, "success", body)
        return True, job, None

    # Unknown state — treat as passive
    log.info("Job %s in unknown state %s, sending success ping", name, state)
    hc_ping(hc_id, "success", f"Job in state {state}")
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
        query_failed = False
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
                    except (TransientError, subprocess.TimeoutExpired) as e:
                        # SSH transport failure, pbs_server unreachable, or query
                        # timeout — the job's state is unknown this cycle. Keep it
                        # in the registry and retry; do NOT declare it dead.
                        log.error("Transient failure querying job %s (state unknown, keeping): %s", job["name"], e)
                        query_failed = True
                        updated_jobs.append(job)

                write_jobs(updated_jobs)
            else:
                log.info("No jobs to monitor")
        finally:
            release_lock(lock_fh)

        # Send pings/emails for disappeared jobs (jobs.json already updated above)
        errors = send_notifications(pending_notifications, api_key, smtp_cfg)

        send_master_ping(master_hc_id, query_failed, errors)

    except Exception:
        log.error("Unhandled exception:\n%s", traceback.format_exc())
        if master_hc_id:
            hc_ping(master_hc_id, "fail", traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
