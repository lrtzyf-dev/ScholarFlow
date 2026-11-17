"""Source-bound requirement candidates and narrowly verifiable user contracts.

Candidates are a coverage aid, never authority. Only explicit user clauses can
create the deterministic contracts below. We retain unknown clauses rather
than claiming that a finite extractor understands every natural-language task.
"""
from __future__ import annotations

import hashlib
import json
import re

PARSER_VERSION = "user-clause-contracts/1"
_NUM = r"(?:\d{1,3}|[一二两三四五六七八九十百]+|one|two|three|four|five|six|seven|eight|nine|ten)"
_NUMBERS = dict(zip("一二三四五六七八九", range(1, 10))) | {"两": 2}
_ENGLISH = dict(zip("one two three four five six seven eight nine ten".split(), range(1, 11)))
_DENY_WRITE = re.compile(
    r"^(?:请|现在|接下来|之后|暂时|记得|务必|但是|但|同时|并且|另外|也)*\s*"
    r"(?:(?:不要|不准|禁止|不得|别|不必|无需|不)(?:再|去|进行)?\s*(?:(?:读取|阅读)(?:或|和|以及))?(?:写入?|修改|改动|创建|新建|保存|导出|生成|删除)"
    r".{0,16}(?:文件|文档|报告|磁盘)|(?:只读|仅阅读|只阅读)(?:检查|分析)?(?:模式)?(?:即可|就好)?)"
    r"|^(?:please\s+)?(?:do\s+not|don't|never|no)\s+(?:read\s+(?:or|and)\s+)?(?:write|modify|change|create|save|export|delete)\b.{0,30}\b(?:files?|documents?|reports?|disk)\b"
    r"|^(?:read[- ]only)(?:\s+mode)?\b", re.I)
_ALLOW_WRITE = re.compile(
    r"^(?:请|现在|接下来|之后)*\s*(?:(?:可以|允许)(?:继续|重新|开始)?写入?(?:项目|本地)?文件|"
    r"(?:解除|取消)(?:此前|之前|刚才|先前)?(?:的)?(?:不要|不准|禁止|不得|不)(?:写入?|修改|创建|保存|导出|删除).{0,12}(?:文件|文档|报告)(?:的)?(?:限制|要求)?)"
    r"|^(?:you\s+may|you\s+can|allow)\s+(?:now\s+)?write\s+(?:project\s+)?files?\b", re.I)
_COMPARE = re.compile(rf"(?:比较|对比|比较分析|compare)\s*(?:这|其中|全部|the|these)?\s*(?:至少|at\s+least\s+)?(?P<n>{_NUM})\s*(?:篇(?:论文|文献|文章)?|papers?|articles?|documents?)", re.I)
_TABLES = re.compile(rf"(?:给出|提供|生成|整理|输出|列出|导出|produce|provide|create|export)\s*(?:至少|at\s+least\s+)?(?P<n>{_NUM})\s*(?:(?:个|张|份)?(?:表格|表)|tables?)", re.I)
_DIRECT_PREFIX = re.compile(r"^(?:(?:请|请你|需要|麻烦|帮我|为我|现在|接下来|先|再|然后|另外|同时|并且|并|还要|改为|改成|改作|变为|完整|逐一|逐个|全部|全文|认真|仔细|按顺序)\s*|(?:please|then|also|instead|now|fully|entirely)\s+)*$", re.I)
_CONDITIONAL_PERMISSION = re.compile(r"如果|假如|等到?|待我|确认后|批准后|同意后|之后|以后|\b(?:if|when|once|after|until|unless)\b", re.I)
_DENY_READ = re.compile(
    r"^(?:请|现在|接下来|之后|暂时|记得|务必|但是|但|同时|并且|另外|也)*\s*"
    r"(?:不要|不准|禁止|不得|别|不必|无需|不)(?:再|去|进行)?\s*(?:读取|阅读|打开|扫描)"
    r"(?:(?:或|和|以及)(?:修改|写入?|改动|删除))?.{0,16}(?:文件|文档|磁盘)"
    r"|^(?:please\s+)?(?:do\s+not|don't|never|no)\s+(?:read|open|scan)\b.{0,30}\b(?:files?|documents?|disk)\b", re.I)
_ALLOW_READ = re.compile(
    r"^(?:请|现在|接下来)*\s*(?:可以|允许)(?:继续|重新|开始)?(?:读取|阅读)(?:项目|本地)?(?:文件|文档)"
    r"|^(?:you\s+may|you\s+can|allow)\s+(?:now\s+)?read\s+(?:project\s+)?files?\b", re.I)


def number(text):
    text = text.casefold()
    if text.isdigit():
        return int(text)
    if text in _ENGLISH:
        return _ENGLISH[text]
    if text in _NUMBERS:
        return _NUMBERS[text]
    if "十" in text and len(text) <= 3:
        left, right = text.split("十", 1)
        return (_NUMBERS.get(left, 1) * 10) + _NUMBERS.get(right, 0)
    return None


def input_candidates(content, message_id, revision):
    """Keep every nonempty clause and its exact Python Unicode character span.

    Quoted text and code remain candidates, but do not activate policy. Long
    clauses remain intact in provenance; model proposals have stricter bounds.
    """
    result = []
    fence = None
    for line in re.finditer(r"[^\n]+", content):
        value = line.group()
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", value)
        if marker and (fence is None or marker[1][0] == fence[0] and len(marker[1]) >= len(fence)
                       and not value[marker.end():].strip()):
            fence = marker[1] if fence is None else None
        quoted = bool(fence or marker or value.startswith(("    ", "\t"))
                      or value.lstrip().startswith((">", '"', "'", "“", "「")))
        for match in re.finditer(r"[^，,；;。！？!?]+(?:[，,；;。！？!?]|$)", value):
            start, end = line.start() + match.start(), line.start() + match.end()
            while start < end and content[start].isspace():
                start += 1
            if start == end:
                continue
            quote = content[start:end]
            body = re.sub(r"^\s*(?:[-*•]|\d+[.)、])\s*", "", quote).strip()
            contract, read_policy = None, None
            if not quoted:
                if not re.search(r"是什么意思|有什么(?:意义|区别)|\b(?:why|what\s+does)\b", body, re.I):
                    if _DENY_READ.search(body):
                        read_policy = {"allowed": False}
                    elif (_ALLOW_READ.search(body) and not _CONDITIONAL_PERMISSION.search(body)
                          and not re.search(r"吗|能否|是否|[?？]", body)):
                        read_policy = {"allowed": True}
                if _DENY_WRITE.search(body):
                    contract = {"kind": "file_write_constraint", "allowed": False}
                elif _ALLOW_WRITE.search(body) and not _CONDITIONAL_PERMISSION.search(body):
                    contract = {"kind": "file_write_constraint", "allowed": True}
                else:
                    for pattern, kind in ((_COMPARE, "compare_source_count"), (_TABLES, "answer_table_count")):
                        found = pattern.search(body)
                        if not found or not _DIRECT_PREFIX.fullmatch(body[:found.start()]):
                            continue
                        amount = number(found.group("n"))
                        if amount is None or not 1 <= amount <= 100:
                            continue
                        if kind == "answer_table_count" and re.search(r"导出|export", found.group(), re.I):
                            kind = "export_table_count"
                        contract = {"kind": kind, "count": amount,
                                    "operator": "at_least" if re.search(r"至少|at\s+least", found.group(), re.I) else "exact"}
                        break
            identity = hashlib.sha256(f"{message_id}:{start}:{end}:{quote}".encode()).hexdigest()
            result.append({"candidate_id": identity, "source_message_id": message_id,
                "source_revision": revision, "start": start, "end": end, "quote": quote,
                "status": "candidate", "origin": "user_text", "contract": contract,
                "file_read_policy": read_policy,
                "quoted_context": quoted,
                "verification": "deterministic_contract" if contract else "not_semantically_verified"})
    return result


def user_inputs(snapshot):
    inputs = []
    for row in snapshot.get("revisions", []):
        if row.get("source") != "user":
            continue
        changes = row.get("changes") or {}
        if row["action"] == "create":
            content = changes.get("description")
        elif row["action"] == "user_input":
            content = changes.get("content")
        else:
            continue
        if isinstance(content, str):
            inputs.append({"source_message_id": row["source_message_id"], "source_revision": row["revision"],
                           "content": content})
    return inputs


def projection(snapshot):
    inputs = user_inputs(snapshot)
    candidates = [candidate for item in inputs for candidate in input_candidates(
        item["content"], item["source_message_id"], item["source_revision"])]
    proposed = [candidate for row in snapshot.get("revisions", []) if row["action"] == "propose_candidates"
                for candidate in row.get("changes", {}).get("candidates", [])]
    constraints = [candidate for candidate in candidates
                   if (candidate.get("contract") or {}).get("kind") == "file_write_constraint"]
    active = constraints[-1] if constraints and not constraints[-1]["contract"]["allowed"] else None
    read_constraints = [candidate for candidate in candidates if candidate.get("file_read_policy")]
    active_read = read_constraints[-1] if read_constraints and not read_constraints[-1]["file_read_policy"]["allowed"] else None
    return {"parser_version": PARSER_VERSION, "inputs": inputs, "candidates": candidates,
            "model_candidates": proposed, "active_file_constraint": active,
            "active_file_read_constraint": active_read,
            "semantic_coverage": "unverified" if inputs else "not_applicable"}


def mask_policy_clauses(content):
    chars = list(content)
    for item in input_candidates(content, "mask", 1):
        if (item.get("contract") or {}).get("kind") == "file_write_constraint":
            chars[item["start"]:item["end"]] = " " * (item["end"] - item["start"])
    return "".join(chars)


def explicit_action(content, pattern):
    """Only an affirmative user imperative may become an execution check.

    Mentioning tests, files, examples or quoted instructions is not a request
    to run a command or write an artifact. Other wording remains a candidate.
    """
    for candidate in input_candidates(content, "action", 1):
        quote = candidate["quote"]
        if candidate.get("quoted_context"):
            continue
        for clause in re.split(r"并(?:且)?|然后|同时|\s+and\s+|\s+then\s+", quote):
            body = re.sub(r"^\s*(?:[-*•]|\d+[.)、])\s*", "", clause).strip()
            if re.search(r"是什么意思|是什么|的(?:步骤|原理|含义|方法)|有什么(?:意义|区别)|\b(?:why|how\s+to|what\s+does)\b", body, re.I):
                continue
            match = re.search(pattern, body, re.I)
            if match and _DIRECT_PREFIX.fullmatch(body[:match.start()]):
                return True
    return False


def tool_constraint(snapshot, name, arguments=None):
    from .goal_contracts import policy_denial
    explicit = policy_denial(snapshot, name)
    if explicit:
        return explicit
    coverage = projection(snapshot)
    reading = coverage["active_file_read_constraint"]
    if reading:
        # This is a narrow, source-bound prohibition, not a topic classifier.
        # Unknown tools/commands cannot promise that they won't open files.
        from .agent.tools import SAFE_SEARCH_TOOLS
        permitted = SAFE_SEARCH_TOOLS | {"update_plan", "respond_in_plan_mode", "deepseek_search", "deepseek_fetch"}
        if name not in permitted:
            return {"blocked_by_goal_constraint": True, "goal_id": snapshot["id"],
                "goal_revision": snapshot["revision"], "source_message_id": reading["source_message_id"],
                "source_span": {key: reading[key] for key in ("start", "end", "quote")},
                "error": "当前用户要求不读取文件，此操作可能读取本地资料，尚未执行。请基于已提供内容回答或规划；确需读取时由用户明确解除限制。"}
    candidate = coverage["active_file_constraint"]
    if not candidate:
        return None
    # Built-in reading may persist application caches/receipts; that is not a
    # request to modify the user's project. Arbitrary commands, hooks, skills
    # and remote tools cannot establish that guarantee from their names.
    from .agent.tools import SAFE_TOOLS, SAFE_SEARCH_TOOLS
    safe = SAFE_TOOLS | SAFE_SEARCH_TOOLS | {"deepseek_search", "deepseek_fetch", "deepseek_ocr",
        "start_research_child", "resume_research_child", "read_research_child"}
    if name in safe:
        return None
    return {"blocked_by_goal_constraint": True, "goal_id": snapshot["id"],
            "goal_revision": snapshot["revision"], "source_message_id": candidate["source_message_id"],
            "source_span": {key: candidate[key] for key in ("start", "end", "quote")},
            "error": "当前用户要求禁止写入文件，此操作可能修改文件，尚未执行。请使用只读工具；仅在确需此操作时澄清或由用户明确解除限制。"}


def count_markdown_tables(answer):
    """Count real displayed table blocks, excluding fenced examples."""
    tables, lines, fence = set(), [], None
    for line in answer.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if marker and (fence is None or marker[1][0] == fence[0] and len(marker[1]) >= len(fence)
                       and not line[marker.end():].strip()):
            fence = marker[1] if fence is None else None
            lines.append("")
            continue
        lines.append("" if fence or line.startswith(("    ", "\t", ">")) else line)
    for index, line in enumerate(lines):
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if (index == 0 or len(cells) < 2 or not all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)
                or "|" not in lines[index - 1]
                or len(lines[index - 1].strip().strip("|").split("|")) != len(cells)):
            continue
        body = []
        for after in lines[index + 1:]:
            row = [cell.strip() for cell in after.strip().strip("|").split("|")]
            if "|" not in after or len(row) != len(cells) or not any(row):
                break
            body.append(row)
        if body:
            tables.add(json.dumps([lines[index - 1], body], ensure_ascii=False))
    return len(tables)


def contract_key(contract):
    return "user_contract:" + json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
