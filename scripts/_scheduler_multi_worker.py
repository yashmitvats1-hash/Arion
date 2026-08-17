#!/usr/bin/env python3
"""Worker for ADR-026 cross-process scheduler tests.

Spawned as a REAL subprocess by tests/test_multi_process_scheduler.py so
claims, leases, heartbeats and crash recovery are exercised across
independent Python processes sharing one SQLite registry DB.

Modes:
  race-claim --db DB --scheduler-id SID [--lease N]
      claim_next() for this scheduler; prints JSON
      {"claimed": work_id|null, "worker": worker_id}; exits 0.
  claim-run --db DB --work-id WID [--lease N] [--sleep S]
      claim() the row, heartbeat once, sleep, mark_terminal COMPLETED as
      the owner; prints JSON {"work_id": ..., "status": ...}; exits 0.
  claim-stop-heartbeat --db DB --work-id WID [--lease N]
      claim() the row, heartbeat once, then exit WITHOUT a terminal
      transition (simulates a worker that stops heartbeating and never
      reports); prints JSON {"work_id": ...}.
  crash-claimed --db DB --work-id WID [--lease N]
      claim() the row, print JSON, then os._exit(1) WITHOUT releasing or
      reporting (dies while RUNNING).
  claim-lock-crash --db DB --sandbox SB [--lease N]
      registry claim + durable mutation-lock acquisition, then os._exit(1)
      (dies holding BOTH the work lease and the mutation lock).
  weighted-claim-run --db DB --work-id WID [--lease N] [--retries N]
      claim() the row (retrying while the ADR-027 weighted gate denies),
      heartbeat, complete as the owner; prints JSON
      {"work_id", "claimed": bool, "attempts": n}; exits 0.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from arion.state.locks import canonical_resource
from arion.state.scheduler_work import SchedulerWorkStatus
from arion.state.store import SQLiteStorage

FS = "filesystem:path"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["race-claim", "claim-run",
                                         "claim-stop-heartbeat",
                                         "crash-claimed", "claim-lock-crash",
                                         "weighted-claim-run",
                                         "claim-once-hold"])
    parser.add_argument("--db", required=True)
    parser.add_argument("--scheduler-id", default=None)
    parser.add_argument("--work-id", default=None)
    parser.add_argument("--lease", type=float, default=60.0)
    parser.add_argument("--max-lease", type=float, default=600.0)
    parser.add_argument("--sleep", type=float, default=0.2)
    parser.add_argument("--retries", type=int, default=200)
    parser.add_argument("--sandbox", default=None)
    args = parser.parse_args()

    store = SQLiteStorage(args.db)
    worker = f"worker:{os.getpid()}:{os.urandom(4).hex()}"

    if args.mode == "race-claim":
        got = store.claim_next(args.scheduler_id, worker_id=worker,
                               lease_seconds=args.lease)
        print(json.dumps({"claimed": got.work_id if got else None,
                          "worker": worker}), flush=True)
        store.close()
        return 0

    if args.mode == "claim-run":
        got = store.claim(args.work_id, worker_id=worker,
                          lease_seconds=args.lease,
                          max_lease_seconds=args.max_lease)
        store.heartbeat(got.work_id, worker, lease_seconds=args.lease,
                        max_lease_seconds=args.max_lease)
        time.sleep(args.sleep)
        store.mark_terminal(got.work_id, SchedulerWorkStatus.COMPLETED,
                            owner_worker_id=worker)
        print(json.dumps({"work_id": got.work_id, "worker": worker,
                          "status": "completed"}), flush=True)
        store.close()
        return 0

    if args.mode == "claim-stop-heartbeat":
        got = store.claim(args.work_id, worker_id=worker,
                          lease_seconds=args.lease,
                          max_lease_seconds=args.max_lease)
        store.heartbeat(got.work_id, worker, lease_seconds=args.lease,
                        max_lease_seconds=args.max_lease)
        print(json.dumps({"work_id": got.work_id, "worker": worker,
                          "status": "running-no-heartbeat"}), flush=True)
        store.close()
        return 0  # no terminal transition: the lease just lapses

    if args.mode == "crash-claimed":
        got = store.claim(args.work_id, worker_id=worker,
                          lease_seconds=args.lease,
                          max_lease_seconds=args.max_lease)
        print(json.dumps({"work_id": got.work_id, "worker": worker,
                          "status": "running"}), flush=True)
        os._exit(1)  # noqa: PLR1722 - deliberate crash while RUNNING

    if args.mode == "claim-once-hold":
        # claim a SPECIFIC row once; report claimed True/False; when
        # claimed, hold the lease for `sleep` seconds and exit WITHOUT a
        # terminal transition (the row stays RUNNING - used by ADR-029
        # rapid-claim tests where the hot goal must hold capacity).
        got = store.claim(args.work_id, worker_id=worker,
                          lease_seconds=args.lease,
                          max_lease_seconds=args.max_lease)
        print(json.dumps({"work_id": args.work_id,
                          "claimed": got is not None, "worker": worker}),
              flush=True)
        if got is not None:
            time.sleep(args.sleep)
        store.close()
        return 0

    if args.mode == "weighted-claim-run":
        from arion.state.scheduler_work import SchedulerStateError

        attempts = 0
        got = None
        while attempts <= int(getattr(args, "retries", 200) or 200):
            attempts += 1
            try:
                got = store.claim(args.work_id, worker_id=worker,
                                  lease_seconds=args.lease,
                                  max_lease_seconds=args.max_lease)
            except SchedulerStateError:
                # raced to a terminal row (fail closed): not claimable
                got = None
                break
            if got is not None:
                break
            time.sleep(0.02)
        if got is None:
            print(json.dumps({"work_id": args.work_id, "worker": worker,
                              "claimed": False, "attempts": attempts}),
                  flush=True)
            store.close()
            return 0
        store.heartbeat(got.work_id, worker, lease_seconds=args.lease,
                        max_lease_seconds=args.max_lease)
        store.mark_terminal(got.work_id, SchedulerWorkStatus.COMPLETED,
                            owner_worker_id=worker)
        print(json.dumps({"work_id": got.work_id, "worker": worker,
                          "claimed": True, "attempts": attempts}), flush=True)
        store.close()
        return 0

    if args.mode == "claim-lock-crash":
        got = store.claim(args.work_id, worker_id=worker,
                          lease_seconds=args.lease,
                          max_lease_seconds=args.max_lease)
        lock = store.acquire(FS, canonical_resource(FS, "a.txt"),
                             "filesystem.write", "write",
                             f"proc:{os.getpid()}", args.lease)
        print(json.dumps({"work_id": got.work_id, "worker": worker,
                          "lock_id": lock.lock_id}), flush=True)
        os._exit(1)  # noqa: PLR1722 - dies holding work lease + mutation lock

    return 1


if __name__ == "__main__":
    sys.exit(main())
