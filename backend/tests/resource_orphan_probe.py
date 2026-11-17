"""Child-process fault probe: real meter, isolated MockTransport, abrupt exits."""
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from sqlalchemy import select
from app import call_ledger, resource_budget
from app.config import DATA_DIR
from app.db import SessionLocal, init_db
from app.ledger_models import CallRecord
from app.llm import APIClient
from app.resource_models import ResourceBudget, ResourceReservation
from app.runtime_context import bind_runtime_config
from app.runtime_owner import owner


def main():
    phase = sys.argv[1]
    owner.begin_service()
    init_db()
    if phase == "recover":
        resource_budget.recover_startup_reservations()
        second = resource_budget.recover_startup_reservations()
        with SessionLocal() as db:
            budget, reservation = db.scalar(select(ResourceBudget)), db.scalar(select(ResourceReservation))
            call = db.scalar(select(CallRecord))
            snapshot = resource_budget.budget_snapshot(budget.id)
            result = {"reservation_state": reservation.state, "reserved_amount": snapshot["reserved_amount"],
                      "spent_amount": snapshot["spent_amount"], "active_seconds": snapshot["active_seconds"],
                      "call_state": call.state if call else None, "call_amount": call.amount if call else None,
                      "dispatches": len((DATA_DIR / "synthetic-dispatches.txt").read_text().splitlines())
                          if (DATA_DIR / "synthetic-dispatches.txt").exists() else 0,
                      "second_recovery": second}
        owner.finish_shutdown(idle=True)
        print(json.dumps(result))
        return
    budget_id = resource_budget.create_budget(owner_run_id="crash-probe", amount_limit="2",
                                             request_estimate="0.4", price_version="synthetic-estimate")
    if phase == "hold":
        resource_budget.reserve(budget_id, run_id="crash-probe")
        owner.ready()
        (DATA_DIR / "holder-started").touch()
        while True:
            time.sleep(0.05)
    target, attribute = {"after_reserve": (resource_budget, "reserve"),
                         "after_call": (call_ledger, "begin_call"),
                         "after_bind": (resource_budget, "bind_call"),
                         "before_settlement": (call_ledger, "finish_call")}.get(phase, (None, None))
    if target:
        original = getattr(target, attribute)
        def crash_after(*args, **kwargs):
            original(*args, **kwargs)
            os._exit(91)
        setattr(target, attribute, crash_after)
    real_client = httpx.Client
    def dispatch(request):
        with (DATA_DIR / "synthetic-dispatches.txt").open("a") as output:
            output.write("synthetic-dispatch\n")
            output.flush()
            os.fsync(output.fileno())
        if phase == "after_dispatch": os._exit(91)
        return httpx.Response(200, json={"choices": [{"message": {"content": "synthetic"}}]})
    httpx.Client = lambda *args, **kwargs: real_client(*args, transport=httpx.MockTransport(dispatch), **kwargs)
    with bind_runtime_config({"run_id": "crash-probe", "resource_budget_id": budget_id}):
        # Exercise the actual production meter around an isolated HTTP outlet;
        # no model credential, external network or actual fee is involved.
        with APIClient(synthetic=True)._meter("https://synthetic.test/chat", "fixture", {}) as receipt:
            with httpx.Client() as client:
                client.post("https://synthetic.test/chat", json={"fixture": True})
    raise AssertionError("fault injection did not exit")


if __name__ == "__main__":
    main()
