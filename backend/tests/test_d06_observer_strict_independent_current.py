"""Current observer contract against preserved pre-fix source, in memory only."""
from copy import deepcopy
from pathlib import Path
import types

import pytest

from scripts import verify_real_acceptance as current
from tests.test_d06_harness_independent_current import event_fixture, response, RUN, CID, ATTEMPT, ANSWER


OLD_SOURCE = Path(__file__).resolve().parents[2] / "verification/d06-harness-review-layers-independent-current/initial-01-inputs/backend/scripts/verify_real_acceptance.py"


@pytest.fixture(scope="module")
def old():
    module = types.ModuleType("d06_preserved_harness_in_memory")
    module.__file__ = str(OLD_SOURCE)
    exec(compile(OLD_SOURCE.read_bytes(), str(OLD_SOURCE), "exec"), module.__dict__)
    return module


def observed(subject, live, terminal):
    return subject.observer_receipt(response(live), response(terminal), run_id=RUN,
        conversation_id=CID, answer=ANSWER, request_after=0, call_count_before=1, call_count_after=1)


@pytest.mark.parametrize("mutation,field", [
    ("missing_initial_attempt", "live_followup_verified"),
    ("bool_initial_seq", "live_followup_verified"),
    ("float_initial_seq", "live_followup_verified"),
    ("different_terminal_attempt", "terminal_no_replay_verified"),
    ("missing_terminal_attempt", "terminal_no_replay_verified"),
    ("stale_terminal_cursor", "terminal_no_replay_verified"),
    ("nonadjacent_fallback_marker", "live_followup_verified"),
])
def test_preserved_old_acceptance_is_rejected_by_current_contract(old, mutation, field):
    live, terminal = event_fixture()
    if mutation == "missing_initial_attempt":
        live[0].pop("attempt_id")
        live[1]["attempt_id"] = "different-output-attempt"
        live[2]["attempt_id"] = "different-terminal-attempt"
    elif mutation == "bool_initial_seq": live[0]["seq"] = False
    elif mutation == "float_initial_seq": live[0]["seq"] = 0.0
    elif mutation == "different_terminal_attempt": terminal[0]["attempt_id"] = "restarted-attempt"
    elif mutation == "missing_terminal_attempt": terminal[0].pop("attempt_id")
    elif mutation == "stale_terminal_cursor": terminal[0].update(cursor=1, seq=1)
    elif mutation == "nonadjacent_fallback_marker":
        final_snapshot = {**deepcopy(terminal[0]), "seq": 2, "cursor": 2}
        live = live[:2] + [final_snapshot,
            {"kind": "thinking", "run_id": RUN, "attempt_id": ATTEMPT, "seq": 3, "text": "Ignored private event"},
            {"kind": "stream_end", "run_id": RUN, "seq": 2, "status": "completed", "terminal": True}]
    assert observed(old, live, terminal)[field] is True, "Preserved old source did not demonstrate this regression"
    assert observed(current, live, terminal)[field] is False


def test_direct_production_fallback_marker_remains_accepted():
    live, terminal = event_fixture()
    live = live[:2] + [deepcopy(terminal[0]),
        {"kind": "stream_end", "run_id": RUN, "seq": 2, "status": "completed", "terminal": True}]
    result = observed(current, live, terminal)
    assert result["live_followup_verified"] is True, result
    assert result["terminal_no_replay_verified"] is True
    assert result["public_events"][-1]["attempt_identity_inherited_from_terminal_snapshot"] is True


def test_output_appearing_only_after_terminal_marker_is_not_live_followup():
    live, terminal = event_fixture()
    live[1]["seq"] = 3
    live = [live[0], live[2], live[1]]
    terminal[0].update(cursor=3, seq=3)
    result = observed(current, live, terminal)
    assert result["live_followup_verified"] is False, result
