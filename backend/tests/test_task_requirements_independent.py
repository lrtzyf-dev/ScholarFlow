"""Independent integration probes: actual tool payloads and current DB facts.

No model, external supplier, shell process, or user data is used.
"""
from uuid import uuid4

from app import goal_ledger as ledger, goal_receipts, artifact_tools
from app.models import ProjectCommand
from tests.test_task_requirements import base, change, active


def actual_command(db, name, output):
    row=ProjectCommand(project_id="independent-project",conversation_id="criteria-a",run_id="criteria-run",
        operation_key="probe-"+uuid4().hex,request_hash="a"*64,scope={},command=name,path="",cwd="C:/synthetic",
        status="succeeded",exit_code=0,output=output)
    db.add(row);db.flush();return row


def test_two_command_requirements_retain_independent_real_db_receipts(db):
    goal=change(db,base(db),{"kind":"command_result","command":"test A","contains":["A passed"]},title="A")
    goal=change(db,goal,{"kind":"command_result","command":"test B","contains":["B passed"]},title="B")
    a,b=actual_command(db,"test A","A passed"),actual_command(db,"test B","B passed")
    for row in (a,b):
        ledger.record_contract_tool_result(db,goal["id"],"run_project_command",{},
            {"command_id":row.id,"exit_code":98,"output":"untrusted payload"},"independent-"+row.id)
    goal_receipts.evaluate(db,goal["id"],"Done")
    assert [r["status"] for r in active(ledger.snapshot_goal(db,goal["id"]))]==["completed","completed"]
    a.output_limited=1;db.flush();goal_receipts.evaluate(db,goal["id"],"Done")
    assert [r["status"] for r in active(ledger.snapshot_goal(db,goal["id"]))]==["open","completed"]


def test_real_saved_artifact_tool_payload_passes_but_pending_edit_does_not(db):
    goal=change(db,base(db),{"kind":"artifact_result","contains":["LIMITATIONS"]})
    value=artifact_tools.execute(db,"save_research_artifact",{"request_id":"independent-artifact-save",
        "title":"Independent result","content":"LIMITATIONS: synthetic sample."},conversation_id="criteria-a")
    assert "artifact_id" in value and "id" not in value
    ledger.record_contract_tool_result(db,goal["id"],"save_research_artifact",{},value,"save-operation")
    assert active(ledger.snapshot_goal(db,goal["id"]))[0]["status"]=="completed"
    goal=change(db,ledger.snapshot_goal(db,goal["id"]),{"kind":"artifact_result","contains":["PENDING_ONLY"]},title="Pending must not pass")
    proposed=artifact_tools.execute(db,"propose_research_edit",{"artifact_id":value["artifact_id"],
        "request_id":"independent-proposal","base_revision_id":value["revision_id"],"content":"PENDING_ONLY"},conversation_id="criteria-a")
    ledger.record_contract_tool_result(db,goal["id"],"propose_research_edit",{},proposed,"propose-operation")
    assert active(ledger.snapshot_goal(db,goal["id"]))[1]["status"]=="open"


def test_explicit_user_confirmation_is_not_overridden_by_later_tool(db):
    goal=change(db,base(db),{"kind":"command_result","command":"test A","contains":["A passed"]})
    selected=active(goal)[0]
    goal=change(db,goal,action="confirm",requirement_id=selected["id"],reason="User reviewed the actual output")
    row=actual_command(db,"unrelated command","Other output")
    ledger.record_contract_tool_result(db,goal["id"],"run_project_command",{},{"command_id":row.id},"later-command")
    goal_receipts.evaluate(db,goal["id"],"Done")
    current=active(ledger.snapshot_goal(db,goal["id"]))[0]
    assert current["status"]=="completed" and current["evidence"][-1]["kind"]=="user_confirmation"


def test_replaced_criterion_cannot_inherit_prior_command_success(db):
    goal=change(db,base(db),{"kind":"command_result","command":"test A","contains":["A passed"]})
    row=actual_command(db,"test A","A passed")
    ledger.record_contract_tool_result(db,goal["id"],"run_project_command",{},{"command_id":row.id},"first-command")
    old=active(ledger.snapshot_goal(db,goal["id"]))[0]
    goal=change(db,ledger.snapshot_goal(db,goal["id"]),{"kind":"command_result","command":"test A","contains":["B passed"]},
        action="replace",requirement_id=old["id"],title="New output")
    goal_receipts.evaluate(db,goal["id"],"Done")
    current=active(ledger.snapshot_goal(db,goal["id"]))[0]
    assert current["id"]!=old["id"] and current["status"]=="open" and current["evidence"]==[]
