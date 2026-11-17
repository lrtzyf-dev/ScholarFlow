"""Bounded public display state and read-only observers for an admitted chat run.

Only events already emitted to the UI enter this store. Executor checkpoints and
credential bindings never do. Token deltas aggregate in memory; the existing
foreground heartbeat persists one compact snapshot rather than one row per token.
"""
from __future__ import annotations

from copy import deepcopy
import json
import queue
import threading
import time

from . import approval_store
from .runtime_context import public_runtime_config

MAX_TEXT = 300_000
MAX_EVENTS = 256
MAX_EVENT_CHARS = 240_000
MAX_EVIDENCE = 256
MAX_EVIDENCE_CHARS = 100_000
OBSERVER_QUEUE_SIZE = 64
FINISHED_TTL = 300.0
MAX_FINISHED = 32
RESYNC = object()

_lock = threading.RLock()
_channels: dict[str, "RunChannel"] = {}
_PRIVATE_KEYS = {"agent_checkpoint", "writer_token", "credential_ref", "credential_id", "credential_version",
                 "llm_api_key", "api_key", "authorization", "bearer_token", "access_token",
                 "refresh_token", "password", "secret", "headers", "_provider", "provider_context",
                 "server_tool_calls", "signature", "stream_extensions"}


def _public(value, *, text_limit=MAX_TEXT, depth=0):
    """Copy public payloads with finite depth/size and omit credential fields."""
    if depth > 12:
        return None
    if isinstance(value, dict):
        return {key: (_public(public_runtime_config(item) if isinstance(item, dict) else None, depth=depth + 1)
                      if key == "runtime_config" else _public(item, text_limit=text_limit, depth=depth + 1))
                for key, item in list(value.items())[:128]
                if isinstance(key, str) and key.lower() not in _PRIVATE_KEYS}
    if isinstance(value, list):
        return [_public(item, text_limit=text_limit, depth=depth + 1) for item in value[:256]]
    if isinstance(value, str):
        return value[:text_limit]
    if value is None or type(value) in (bool, int, float):
        return value
    return str(value)[:text_limit]


def _public_evidence(rows):
    if not isinstance(rows, list):
        return []
    result, size = [], 0
    for item in rows[:MAX_EVIDENCE]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        candidate = _public(item, text_limit=4_000)
        row_size = len(json.dumps(candidate, ensure_ascii=False))
        if size + row_size > MAX_EVIDENCE_CHARS:
            break
        result.append(candidate)
        size += row_size
    return result


def initial_snapshot(run_id: str, conversation_id: str | None, saved=None, *, partial_answer="",
                     status="running", phase="setup", error="", runtime_config=None,
                     attempt_id=None, goal_id=None):
    saved = saved if isinstance(saved, dict) else {}
    cursor = saved.get("cursor", saved.get("seq", 0))
    cursor = cursor if type(cursor) is int and cursor >= 0 else 0
    events = _public(saved.get("events") or [], text_limit=20_000)
    thinking_parts = [str(text)[:MAX_TEXT] for text in (saved.get("thinking_parts") or [])[-64:]]
    # Older releases filed public tool preambles as thinking. Keep their event
    # for timeline compatibility, but remove one corresponding archived part.
    for event in events:
        if isinstance(event, dict) and event.get("kind") == "thinking" and event.get("preamble"):
            text = event.get("text")
            if text in thinking_parts:
                thinking_parts.remove(text)
    return {
        "kind": "snapshot", "run_id": run_id, "conversation_id": conversation_id,
        "attempt_id": attempt_id or saved.get("attempt_id"),
        "goal_id": goal_id or saved.get("goal_id"),
        "seq": cursor, "cursor": cursor, "status": status, "phase": phase,
        "error": error or "", "terminal": status != "running",
        "partial_answer": str(saved.get("partial_answer", partial_answer) or "")[:MAX_TEXT],
        # Evidence arrives before the final answer so a live draft can render
        # working citation links. Keep an authoritative bounded copy outside
        # the rolling event tail, which may evict older evidence events.
        "evidence": _public_evidence(saved.get("evidence") or []),
        "review_progress": _public(saved.get("review_progress")) if saved.get("review_progress") else None,
        "thinking_parts": thinking_parts,
        "thinking": str(saved.get("thinking") or "")[:MAX_TEXT],
        "events": events,
        "plan_steps": _public(saved.get("plan_steps") or []),
        "result": _public(saved.get("result")) if saved.get("result") else None,
        "history_truncated": bool(saved.get("history_truncated")),
        "runtime_config": public_runtime_config(runtime_config) or public_runtime_config(saved.get("runtime_config")),
        "approvals": [],
    }


class RunChannel:
    def __init__(self, run_id, conversation_id, saved=None, *, runtime_config=None, durable=True,
                 reset_answer=False, attempt_id=None, goal_id=None):
        self.lock = threading.RLock()
        self.state = initial_snapshot(run_id, conversation_id, saved, runtime_config=runtime_config,
                                      attempt_id=attempt_id, goal_id=goal_id)
        self.state.update(status="running", terminal=False, error="", result=None)
        if reset_answer:
            self.state["partial_answer"] = ""
            self.state["evidence"] = []
            self.state["review_progress"] = None
        self.subscribers: set[queue.Queue] = set()
        self.closed_at = None
        self.durable = durable
        self.event_chars = sum(len(json.dumps(event, ensure_ascii=False)) for event in self.state["events"])

    def _snapshot(self):
        state = deepcopy(self.state)
        state["approvals"] = approval_store.list_for_run(state["run_id"]) if not state["terminal"] else []
        return state

    def snapshot(self):
        with self.lock:
            return self._snapshot()

    def subscribe(self):
        """Taking the initial snapshot and registering its tail is atomic."""
        observer = queue.Queue(maxsize=OBSERVER_QUEUE_SIZE)
        with self.lock:
            snapshot = self._snapshot()
            self.subscribers.add(observer)
        return snapshot, observer

    def unsubscribe(self, observer):
        with self.lock:
            self.subscribers.discard(observer)

    def record(self, kind, event):
        # This copies this event only, not the growing accumulated answer.
        if kind == "evidence" and isinstance(event, dict):
            event = {**event, "evidence": _public_evidence(event.get("evidence"))}
        public = _public(event, text_limit=4_000 if kind == "evidence" else MAX_TEXT)
        public = public if isinstance(public, dict) else {}
        with self.lock:
            state = self.state
            state["cursor"] += 1
            state["seq"] = state["cursor"]
            public.update(kind=kind, seq=state["cursor"], run_id=state["run_id"],
                          attempt_id=state["attempt_id"], goal_id=state["goal_id"])
            if kind == "answer_delta":
                text = state["partial_answer"] + str(public.get("text") or "")
                state["history_truncated"] |= len(text) > MAX_TEXT
                state["partial_answer"] = text[:MAX_TEXT]
            elif kind == "answer_reset":
                state["partial_answer"] = ""
                state["result"] = None
                state["evidence"] = []
                state["review_progress"] = None
            elif kind == "evidence":
                incoming = public.get("evidence") if isinstance(public.get("evidence"), list) else []
                merged = state.setdefault("evidence", [])
                known = {item.get("id") for item in merged if isinstance(item, dict)}
                size = sum(len(json.dumps(item, ensure_ascii=False)) for item in merged)
                for item in incoming:
                    if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
                            or item["id"] in known or len(merged) >= MAX_EVIDENCE):
                        continue
                    candidate = _public(item, text_limit=4_000)
                    candidate_size = len(json.dumps(candidate, ensure_ascii=False))
                    if size + candidate_size > MAX_EVIDENCE_CHARS:
                        break
                    merged.append(candidate)
                    known.add(candidate["id"])
                    size += candidate_size
            elif kind == "review_progress":
                state["review_progress"] = deepcopy(public)
            elif kind == "commentary" and public.get("draft_to_commentary"):
                # The text was streamed provisionally as an answer until the
                # provider confirmed tool calls. It now lives once in events,
                # independently from private/provider reasoning display state.
                state["partial_answer"] = ""
                state["result"] = None
            elif kind == "thinking_delta":
                text = state["thinking"] + str(public.get("text") or "")
                state["history_truncated"] |= len(text) > MAX_TEXT
                state["thinking"] = text[:MAX_TEXT]
            elif kind == "thinking":
                text = str(public.get("text") or "")
                if public.get("preamble"):
                    state["partial_answer"] = ""
                    state["result"] = None
                else:
                    state["thinking_parts"].append(text)
                    state["thinking"] = ""
                    while len(state["thinking_parts"]) > 64 or sum(map(len, state["thinking_parts"])) > MAX_TEXT:
                        state["thinking_parts"].pop(0)
                        state["history_truncated"] = True
            elif kind == "answer":
                state["partial_answer"] = str(public.get("answer") or "")[:MAX_TEXT]
                state["review_progress"] = None
                if state["thinking"]:
                    state["thinking_parts"].append(state["thinking"])
                    while len(state["thinking_parts"]) > 64 or sum(map(len, state["thinking_parts"])) > MAX_TEXT:
                        state["thinking_parts"].pop(0)
                        state["history_truncated"] = True
                state["thinking"] = ""
                state["result"] = deepcopy(public)
            if kind in ("plan", "todo_update") and isinstance(public.get("steps"), list):
                state["plan_steps"] = deepcopy(public["steps"])
            if public.get("runtime_config"):
                state["runtime_config"] = public_runtime_config(public["runtime_config"])
            if kind == "error":
                state["error"] = str(public.get("text") or "")[:2000]
            state["phase"] = {"answer_delta": "answer", "thinking_delta": "thinking",
                               "evidence": "answer", "review_progress": "finalize"}.get(kind, kind)[:32]
            if kind not in ("answer_delta", "thinking_delta", "answer", "done", "stream_end"):
                entry = _public(public, text_limit=20_000)
                size = len(json.dumps(entry, ensure_ascii=False))
                if size <= MAX_EVENT_CHARS:
                    state["events"].append(entry)
                    self.event_chars += size
                else:
                    state["history_truncated"] = True
                while len(state["events"]) > MAX_EVENTS or self.event_chars > MAX_EVENT_CHARS:
                    self.event_chars -= len(json.dumps(state["events"].pop(0), ensure_ascii=False))
                    state["history_truncated"] = True
            return public

    def broadcast(self, event):
        with self.lock:
            for observer in tuple(self.subscribers):
                try:
                    observer.put_nowait(event)
                except queue.Full:
                    # Coalesce a slow view to a fresh authoritative snapshot.
                    # It never backpressures the model or builds an endless queue.
                    while True:
                        try:
                            observer.get_nowait()
                        except queue.Empty:
                            break
                    observer.put_nowait(RESYNC)

    def end(self, status, error=""):
        with self.lock:
            self.state.update(status=status, terminal=True, error=error or self.state["error"])
            self.closed_at = time.monotonic()
            return self.record("stream_end", {"status": status, "terminal": True, "error": self.state["error"]})


def _prune():
    now = time.monotonic()
    finished = sorted(((key, channel) for key, channel in _channels.items()
                       if channel.closed_at is not None), key=lambda item: item[1].closed_at)
    for index, (key, channel) in enumerate(finished):
        if not channel.durable or now - channel.closed_at > FINISHED_TTL or index < len(finished) - MAX_FINISHED:
            _channels.pop(key, None)


def begin(run_id, conversation_id, saved=None, **kwargs):
    with _lock:
        _prune()
        channel = RunChannel(run_id, conversation_id, saved, **kwargs)
        _channels[run_id] = channel
        return channel


def get(run_id):
    with _lock:
        _prune()
        return _channels.get(run_id)


def release_finished():
    with _lock:
        _prune()


def forget_conversation(conversation_id):
    """Discard finished in-memory plaintext after its durable history is deleted."""
    with _lock:
        for run_id, channel in list(_channels.items()):
            with channel.lock:
                if channel.state.get("conversation_id") != conversation_id:
                    continue
                channel.state = initial_snapshot(run_id, conversation_id, status="deleted", error="会话已删除")
                channel.closed_at = time.monotonic()
                channel.event_chars = 0
                for observer in channel.subscribers:
                    while True:
                        try: observer.get_nowait()
                        except queue.Empty: break
                    observer.put_nowait(deepcopy(channel.state))
                _channels.pop(run_id, None)


def display_snapshot(run):
    """Expose current public state, never the private agent checkpoint."""
    channel = get(run.id)
    if channel is not None and channel.state["conversation_id"] == run.conversation_id:
        return channel.snapshot()
    snapshot = initial_snapshot(run.id, run.conversation_id, run.display_snapshot,
                                partial_answer=run.partial_answer, status=run.status,
                                phase=run.phase, error=run.error, runtime_config=run.runtime_config)
    from sqlalchemy.orm import object_session
    from .run_lifecycle import public_run
    session = object_session(run)
    identity = public_run(session, run.id) if session is not None else None
    if identity:
        snapshot.update(attempt_id=identity["attempt_id"], goal_id=identity["goal_id"])
    snapshot["approvals"] = approval_store.list_for_run(run.id) if run.status == "running" else []
    return snapshot
