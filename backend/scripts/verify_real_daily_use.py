"""Opt-in real-provider smoke evaluation using synthetic materials only.

Never a pytest test: invoke explicitly with --run-real and a configured data
directory. Credentials are read once in memory; no user library is opened and
no existing service or application data is modified. Reports omit secrets and
private model reasoning. This is bounded smoke evidence, not release approval.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-real", action="store_true", required=True)
    parser.add_argument("--credentials-dir", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--case", choices=["daily_greeting", "everyday_reasoning", "fixed_attachment_evidence",
        "topic_switch", "plan_mode_direct_answer", "plan_mode_reviewable_plan"])
    parser.add_argument("--include-plan-smoke", action="store_true",
                        help="Also verify direct answers and reviewable plans in plan mode, within the same request budget")
    args = parser.parse_args()
    backend = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(backend))
    from app.credential_protection import unprotect
    saved = json.loads((Path(args.credentials_dir) / "config.json").read_text(encoding="utf-8"))
    protected = (saved.get("protected_credentials") or {}).get("llm_api_key")
    key = unprotect(protected) if protected else saved.get("llm_api_key", "")
    if not key:
        raise RuntimeError("ConfiguredCredentialUnavailable")
    base, model = saved.get("llm_base_url", ""), saved.get("llm_model", "")
    from urllib.parse import urlsplit
    if urlsplit(base).scheme != "https" or urlsplit(base).hostname != "api.deepseek.com":
        raise RuntimeError("ThisEvaluationRequiresConfiguredOfficialDeepSeekEndpoint")
    report = {"kind": "real_model_smoke", "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "model_id": model, "provider": "api.deepseek.com", "synthetic_material_only": True,
              "private_documents_sent": False, "maximum_real_requests": 16,
              "scope": "small_daily_use_sample_not_release_approval", "cases": []}
    source_paths = sorted(path.relative_to(backend).as_posix() for path in (backend / "app").rglob("*.py"))
    source_paths.append("scripts/verify_real_daily_use.py")
    def hashes():
        return {name: hashlib.sha256((backend / name).read_bytes()).hexdigest() for name in source_paths}
    report["source_hashes_at_start"] = hashes()
    report_path = Path(args.report).resolve()
    def write_report():
        report_path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(report, ensure_ascii=False, indent=2)
        if key in encoded:
            raise RuntimeError("SecretRejectedFromReport")
        report_path.write_text(encoded, encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="scholarflow_real_eval_") as temporary:
        os.environ.update(DATA_DIR=str(Path(temporary) / "data"), APPDATA=str(Path(temporary) / "appdata"),
            LOCALAPPDATA=str(Path(temporary) / "localappdata"), LLM_API_KEY=key, LLM_BASE_URL=base,
            LLM_MODEL=model, EMBEDDING_API_KEY="", REDIS_URL="", DESKTOP_NOTIFICATIONS="false",
            MEMORY_CAPTURE="false", SCHOLARFLOW_TEST_TRANSPORT="1")
        from app.main import app
        from app import approval_store, llm
        from app.call_ledger import summarize_calls
        from app.config import settings
        from app.db import engine
        from fastapi.testclient import TestClient
        settings.approval_mode = "read_only"
        settings.reasoning_effort = ""
        real_meter = llm.APIClient._meter
        meter_lock = threading.Lock()
        sent = 0
        @contextmanager
        def limited_meter(self, *values, **kwargs):
            nonlocal sent
            with meter_lock:
                if sent >= 16:
                    raise RuntimeError("EvaluationRequestBudgetExceeded")
                sent += 1
            self._timeout = 90
            with real_meter(self, *values, **kwargs) as receipt:
                yield receipt
        llm.APIClient._meter = limited_meter
        original_approval = approval_store.create
        denied = []
        def deny_external(*values, **kwargs):
            approval = original_approval(*values, **kwargs)
            denied.append(approval.tool_name)
            approval_store.resolve(approval.id, "deny")
            return approval
        approval_store.create = deny_external
        try:
            with TestClient(app) as client:
                cid = client.post("/api/conversations", json={"title": "真实模型合成验收"}).json()["id"]
                raw = ("合成验收记录，不是真实研究。\n"
                       "实验A：样本量24，准确率81%。\n实验B：样本量36，准确率87%。\n"
                       "本记录没有置信区间、p值或显著性检验结果。\n").encode("utf-8")
                upload = client.post("/api/sources/upload", files={"file": ("合成实验记录.txt", raw)},
                                     data={"conversation_id": cid, "request_id": "real-eval-synthetic-source"})
                if upload.status_code != 200:
                    raise RuntimeError("SyntheticAttachmentUploadFailed")
                revision = upload.json()["revision_id"]
                cases = [
                    ("daily_greeting", "你好，简单介绍一下你能帮我做什么，80字以内。", []),
                    ("everyday_reasoning", "我上午有90分钟，读书用三分之一，剩下的时间一半整理房间、一半休息。每项各多少分钟？请直接回答，不需要查资料。", []),
                    ("fixed_attachment_evidence", "请阅读本轮附件中的合成实验记录，列出A和B各自的样本量与准确率、两者准确率相差多少个百分点，并指出能否据此断言统计显著。引用原文，180字以内。", [revision]),
                    ("topic_switch", "先不讨论刚才的实验，帮我给朋友写一句简短的生日祝福，亲切自然，不提科研。", []),
                ]
                if args.include_plan_smoke or (args.case or '').startswith('plan_mode_'):
                    cases.extend([
                        ("plan_mode_direct_answer", "用一个简单例子解释为什么应定期备份电脑文件，80字以内。这里只需直接解释，不需要制定任务计划。", []),
                        ("plan_mode_reviewable_plan", "请为下周整理五篇已下载的论文制定可确认执行的计划，目标是比较研究问题、方法与局限，最后导出一份对照表。这轮只制定计划，不读取或修改文件。", []),
                    ])
                if args.case:
                    cases = [c for c in cases if c[0] == args.case]
                for name, question, revisions in cases:
                    began, before = time.monotonic(), sent
                    payload = {"question": question, "conversation_id": cid, "source_revision_ids": revisions}
                    if name.startswith("plan_mode_"):
                        payload["execution_mode"] = "plan"
                    response = client.post("/api/chat/stream", json=payload)
                    events = []
                    if response.status_code == 200:
                        for line in response.text.splitlines():
                            if line.startswith("data:"):
                                events.append(json.loads(line[5:]))
                    answers = [e for e in events if e.get("kind") == "answer"]
                    final = answers[-1] if answers else {}
                    answer = final.get("answer", "")
                    evidence = final.get("evidence", [])
                    tools = [e.get("name") for e in events if e.get("kind") == "tool_call"]
                    checks = {"http_ok": response.status_code == 200,
                              "delivered_answer": bool(answer), "no_refusal": not final.get("refusal", 0)}
                    if name in ("daily_greeting", "everyday_reasoning", "topic_switch"):
                        checks["no_literature_or_attachment_lookup"] = not any(t in {
                            "retrieve_evidence", "list_papers", "read_attached_source", "list_attached_sources"} for t in tools)
                    if name == "everyday_reasoning":
                        checks["correct_time_allocation"] = "30" in answer and "60分钟读书" not in answer
                    if name == "fixed_attachment_evidence":
                        checks.update({"actually_read_attachment": "read_attached_source" in tools,
                            "correct_source_values": all(v in answer for v in ("24", "36", "81", "87", "6")),
                            "fixed_source_citation": any(e.get("revision_id") == revision and e.get("locator_id") for e in evidence),
                            "citation_marker": "[^e" in answer,
                            "avoids_unsupported_significance": any(v in answer for v in ("不能", "无法", "不足", "未提供", "没有")) and "显著" in answer})
                    if name == "topic_switch":
                        checks["responds_to_new_topic"] = "生日" in answer and "81%" not in answer
                    if name == "plan_mode_direct_answer":
                        checks.update({"direct_response_completed": final.get("completed") is True and final.get("outcome") == "completed",
                            "no_fake_plan": not final.get("plan_steps"),
                            "no_plan_failure_notice": "规划未完成" not in answer,
                            "no_external_action": not tools})
                    if name == "plan_mode_reviewable_plan":
                        checks.update({"reviewable_plan_created": final.get("outcome") == "planned" and bool(final.get("plan_steps")),
                            "execution_not_claimed": final.get("completed") is False,
                            "structured_plan_registered": "update_plan" in tools,
                            "no_external_action": all(tool == "update_plan" for tool in tools)})
                    result = {"name": name, "elapsed_seconds": round(time.monotonic() - began, 2),
                              "real_requests": sent - before, "answer": answer, "tools": tools,
                              "checks": checks, "passed": all(checks.values()),
                              "event_counts": dict(Counter(e.get("kind", "unknown") for e in events)),
                              "evidence_count": len(evidence), "gate": final.get("gate"),
                              "outcome": final.get("outcome"), "completed": final.get("completed"),
                              "diagnostics": [e for e in events if e.get("kind") in ("critic", "audit", "refuse", "tool_result", "error")],
                              "model_answer_draft": "".join(e.get("text", "") for e in events if e.get("kind") == "answer_delta")}
                    report["cases"].append(result)
                    write_report()
                    print(json.dumps({"case": name, "passed": result["passed"], "real_requests": result["real_requests"], "checks": checks}, ensure_ascii=False), flush=True)
                    if not answer:
                        break  # Do not repeatedly bill an unusable configuration.
                report["usage"] = summarize_calls()
                report["real_requests"] = sent
                report["denied_external_tools"] = denied
                report["passed"] = len(report["cases"]) == len(cases) and all(c["passed"] for c in report["cases"])
        except Exception as exc:
            report["error_kind"] = type(exc).__name__
            report["passed"] = False
            report["real_requests"] = sent
            raise
        finally:
            llm.APIClient._meter = real_meter
            approval_store.create = original_approval
            report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            report["source_hashes_at_finish"] = hashes()
            report["source_unchanged_during_evaluation"] = report["source_hashes_at_start"] == report["source_hashes_at_finish"]
            write_report()
            engine.dispose()
    print(json.dumps({"report": str(report_path), "passed": report.get("passed"), "real_requests": report.get("real_requests")}, ensure_ascii=False))
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"evaluation_error": type(exc).__name__}), flush=True)
        raise SystemExit(2) from None
