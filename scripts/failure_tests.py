"""Failure tests against the deployed system (ADR-0018): what happens to a dispute when its
worker is stopped, killed, or handed a message it can never process.

    graceful   delete the worker pods politely, as a deploy does, while a run is in progress
    poison     queue a dispute whose stored request cannot be read
    kill       force-kill the worker pods mid-run (takes about 5 minutes: the queue's lock)

Usage: make failure-tests           (graceful and poison)
       make failure-test-kill       (the 5-minute one)
Needs a port-forward to the supervisor on :18004, which the make targets set up.
"""

import asyncio
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from demo_token import issue  # noqa: E402

URL = "http://127.0.0.1:18004/v1/disputes"
NAMESPACE = os.environ.get("K8S_NAMESPACE", "disputes")
WORKERS = "app.kubernetes.io/name=triage-worker"


def kubectl(*args: str) -> str:
    return subprocess.run(["kubectl", "-n", NAMESPACE, *args], capture_output=True, text=True).stdout.strip()


def psql(sql: str) -> str:
    return kubectl("exec", "postgres-0", "--", "psql", "-U", "disputes", "-d", "disputes", "-Atc", sql)


def audit_trail(dispute_id: str) -> str:
    return psql("SELECT to_char(at,'HH24:MI:SS') || '  ' || rpad(execution_status,9) || rpad(business_status,24) || note "
                f"FROM dispute_events WHERE dispute_id='{dispute_id}' ORDER BY event_id")


def submit(http: httpx.Client, transaction_id: str) -> str:
    body = {"transaction_id": transaction_id, "reason": "failed_transfer", "claimed_amount": "50000.00"}
    return http.post(URL, json=body, headers=auth()).json()["dispute_id"]


def auth() -> dict:
    return {"Authorization": f"Bearer {issue('user-1001')}"}  # a fresh token each time: the kill test outlives one


def wait_for(http: httpx.Client, dispute_id: str, wanted: tuple[str, ...], timeout: float) -> tuple[dict, float]:
    started = time.monotonic()
    while True:
        view = http.get(f"{URL}/{dispute_id}", headers=auth()).json()
        if view["execution_status"] in wanted or time.monotonic() - started > timeout:
            return view, time.monotonic() - started
        time.sleep(0.5)


def graceful(http: httpx.Client) -> bool:
    dispute_id = submit(http, "TX-20261001000001")
    wait_for(http, dispute_id, ("running",), 30)
    print(f"dispute {dispute_id[:8]} is running; deleting both worker pods politely (what a deploy does)")
    kubectl("delete", "pod", "-l", WORKERS, "--wait=false")
    view, took = wait_for(http, dispute_id, ("finished", "failed"), 120)
    print(f"-> {view['execution_status']} / {view['status']} after {took:.1f} s\n{audit_trail(dispute_id)}")
    return view["execution_status"] == "finished" and "restarted" not in audit_trail(dispute_id)


def kill(http: httpx.Client) -> bool:
    dispute_id = submit(http, "TX-20261001000008")
    wait_for(http, dispute_id, ("running",), 30)
    print(f"dispute {dispute_id[:8]} is running; killing both worker pods with no warning (a crash)")
    kubectl("delete", "pod", "-l", WORKERS, "--grace-period=0", "--force")
    time.sleep(20)
    view = http.get(f"{URL}/{dispute_id}", headers=auth()).json()
    print(f"20 s later: {view['execution_status']} / {view['status']} | customer sees: {view['customer_message']}")
    view, took = wait_for(http, dispute_id, ("finished", "failed"), 420)
    attempts = psql(f"SELECT attempts FROM disputes WHERE dispute_id='{dispute_id}'")
    print(f"-> {view['execution_status']} / {view['status']} after {took + 20:.0f} s; attempts = {attempts}\n{audit_trail(dispute_id)}")
    return view["execution_status"] == "finished" and attempts == "2"


def poison(http: httpx.Client) -> bool:
    from services.supervisor.queue import ServiceBusQueue

    dispute_id = str(uuid.uuid4())
    psql("INSERT INTO disputes (dispute_id, dispute_key, user_id, transaction_id, request, execution_status, "
         f"business_status, customer_message) VALUES ('{dispute_id}', 'dispute:poison-{dispute_id[:8]}', 'user-1001', "
         "'TX-20261001000001', '{\"broken\": true}', 'queued', 'received', 'We''ve received your dispute.')")

    async def send_and_check() -> bool:
        queue = ServiceBusQueue(os.environ["SERVICEBUS_NAMESPACE"], os.environ["SERVICEBUS_QUEUE"])
        try:
            await queue.send(uuid.UUID(dispute_id))
            await asyncio.sleep(25)
            return uuid.UUID(dispute_id) in await queue.dead_letter_ids()
        finally:
            await queue.close()

    print(f"queued a dispute whose stored request cannot be read ({dispute_id[:8]}); waiting 25 s")
    dead_lettered = asyncio.run(send_and_check())
    row = psql("SELECT execution_status || ' / ' || business_status || ' / attempts=' || attempts "
               f"FROM disputes WHERE dispute_id='{dispute_id}'")
    print(f"in the dead-letter queue: {dead_lettered}\ndispute: {row}\n{audit_trail(dispute_id)}")
    return dead_lettered and row == "failed / pending_human_approval / attempts=2"


if __name__ == "__main__":
    tests = {"graceful": graceful, "poison": poison, "kill": kill}
    if len(sys.argv) != 2 or sys.argv[1] not in tests:
        sys.exit(f"usage: failure_tests.py {' | '.join(tests)}")
    with httpx.Client(timeout=60) as client:
        passed = tests[sys.argv[1]](client)
    print("PASS" if passed else "FAIL")
    sys.exit(0 if passed else 1)
