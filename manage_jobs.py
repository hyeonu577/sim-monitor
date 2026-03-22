#!/usr/bin/env python3
"""Add or remove jobs from the sim-monitor registry."""

import argparse
import fcntl
import json
import os
import sys

import requests
from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
JOBS_PATH = os.path.join(SCRIPT_DIR, "jobs.json")
LOCK_PATH = os.path.join(SCRIPT_DIR, ".jobs.json.lock")
ENV_PATH = os.path.join(SCRIPT_DIR, ".env")

load_dotenv(ENV_PATH)

HC_API_URL = "https://healthchecks.io/api/v3/checks/"
HC_TIMEOUT = 10


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
            "channels": args.channels,
        }
        jobs.append(job)
        write_jobs(jobs)
        print(f"Added job '{args.name}' ({args.job})")
    finally:
        release_lock(lock_fh)



def hc_delete(api_key, uuid):
    """Delete a healthcheck from healthchecks.io."""
    try:
        resp = requests.delete(
            HC_API_URL + uuid,
            headers={"X-Api-Key": api_key},
            timeout=HC_TIMEOUT,
        )
        resp.raise_for_status()
        print(f"Deleted healthcheck {uuid}")
    except Exception as e:
        print(f"Warning: failed to delete healthcheck {uuid}: {e}", file=sys.stderr)


def remove_job(args):
    lock_fh = acquire_lock()
    try:
        jobs = read_jobs()
        removed = [j for j in jobs if j["name"] == args.name]
        remaining = [j for j in jobs if j["name"] != args.name]
        if not removed:
            print(f"Error: no job with name '{args.name}' found", file=sys.stderr)
            sys.exit(1)
        write_jobs(remaining)
        print(f"Removed job '{args.name}'")
    finally:
        release_lock(lock_fh)

    # Delete healthcheck after releasing lock
    hc_id = removed[0].get("healthcheck_id")
    if hc_id:
        api_key = os.environ.get("HEALTHCHECK_API_KEY")
        if api_key:
            hc_delete(api_key, hc_id)
        else:
            print("Warning: HEALTHCHECK_API_KEY not found in .env, skipping healthcheck deletion", file=sys.stderr)


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


def list_channels(args):
    api_key = os.environ.get("HEALTHCHECK_API_KEY")
    if not api_key:
        print("Error: HEALTHCHECK_API_KEY not found in .env", file=sys.stderr)
        sys.exit(1)
    resp = requests.get(
        HC_API_URL.replace("/checks/", "/channels/"),
        headers={"X-Api-Key": api_key},
        timeout=HC_TIMEOUT,
    )
    resp.raise_for_status()
    channels = resp.json().get("channels", [])
    if not channels:
        print("No notification channels configured on healthchecks.io.")
        return
    for ch in channels:
        print(f"  {ch['name']:30s}  kind={ch['kind']:10s}  id={ch['id']}")


def main():
    parser = argparse.ArgumentParser(description="Manage sim-monitor job registry.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="Add a job to monitor")
    p_add.add_argument("--job", required=True, help="PBS job ID (e.g., 12345.pbs-host)")
    p_add.add_argument("--name", required=True, help="Human-readable job name")
    p_add.add_argument("--output-dir", required=True, help="Path to simulation output directory")
    p_add.add_argument("--output-pattern", required=True, help="Glob pattern for output files (e.g., DD*)")
    p_add.add_argument("--stale-timeout", type=int, required=True, help="Minutes without output before stale")
    p_add.add_argument("--channels", default="*", help="Healthchecks.io notification channels (comma-separated UUIDs/names, or '*' for all)")

    p_rm = sub.add_parser("remove", help="Remove a job from monitoring")
    p_rm.add_argument("name", help="Job name to remove")

    sub.add_parser("list", help="List all monitored jobs")

    sub.add_parser("list-channels", help="List available healthchecks.io notification channels")

    args = parser.parse_args()

    if args.command == "add":
        add_job(args)
    elif args.command == "remove":
        remove_job(args)
    elif args.command == "list":
        list_jobs(args)
    elif args.command == "list-channels":
        list_channels(args)


if __name__ == "__main__":
    main()
