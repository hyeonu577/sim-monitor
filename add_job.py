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
        jobs = [j for j in jobs if j["name"] != args.name]
        if len(jobs) == original_len:
            print(f"Error: no job with name '{args.name}' found", file=sys.stderr)
            sys.exit(1)
        write_jobs(jobs)
        print(f"Removed job '{args.name}'")
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
