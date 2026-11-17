"""Narrow affirmative export clauses with exact user provenance, not NLP authority."""
import re

from .goal_candidates import explicit_action, input_candidates

PARSER_VERSION = "fixed-artifact-export-clauses/1"
_ACTION = r"(?:生成|创建|新建|保存|导出|produce|create|save|export)"
_FORMAT = r"(?:(?<![A-Za-z0-9])(?:docx|word|xlsx|svg)(?![A-Za-z0-9])|复算包|reproducibility\s+bundle)"


def project_destination(content):
    """An explicit project/path obligation still needs the project writer."""
    from .goal_candidates import mask_policy_clauses
    text = mask_policy_clauses(content)
    return (explicit_action(text, r"(?:写入|保存到|导出到|创建到|save\s+(?:to|in)|write\s+to).{0,24}(?:项目|目录|路径|project|folder|directory)")
        or bool(re.search(r"(?:[A-Za-z]:[\\/]|(?:[\w.-]+[\\/])+)[\w.-]+\.(?:docx|xlsx|svg|zip)\b", text, re.I)))


def export_clauses(content, message_id="export", revision=1):
    result = []
    for candidate in input_candidates(content, message_id, revision):
        quote = candidate["quote"]
        if (candidate["quoted_context"] or project_destination(quote)
                or re.search(r"吗|能否|是否|[?？]|如果|假如|确认后|批准后|\b(?:if|when|once|after|until|unless)\b", quote, re.I)
                or not explicit_action(quote, rf"{_ACTION}.{{0,24}}{_FORMAT}")):
            continue
        formats = []
        for match in re.finditer(_FORMAT, quote, re.I):
            value = re.sub(r"\s+", " ", match.group().casefold())
            fmt = "docx" if value == "word" else "bundle" if value in ("复算包", "reproducibility bundle") else value
            if fmt not in formats:
                formats.append(fmt)
        for fmt in formats:
            result.append({**candidate, "result_contract": {"kind": "artifact_export_result", "format": fmt},
                "parser_version": PARSER_VERSION})
    return result


def project_file_text(content):
    """Mask only recognized download clauses, retaining other file obligations."""
    chars = list(content)
    for candidate in export_clauses(content):
        quote = candidate["quote"]
        # Mixed or unsupported output formats remain under the original file
        # check. Recognizing DOCX must not erase a separate PDF/code deliverable.
        if (re.search(r"(?<![A-Za-z])(?:pdf|pptx?|html|csv|tsv|markdown|python)(?![A-Za-z])", quote, re.I)
                or any(extension.casefold() not in ("docx", "xlsx", "svg", "zip")
                       for extension in re.findall(r"\w+\.([A-Za-z0-9]+)", quote))):
            continue
        # A leading space-only mask would turn the whole line into an indented
        # quote, hiding an affirmative obligation later on the same line.
        width = candidate["end"] - candidate["start"]
        chars[candidate["start"]:candidate["end"]] = "；" + " " * (width - 1)
    return "".join(chars)
