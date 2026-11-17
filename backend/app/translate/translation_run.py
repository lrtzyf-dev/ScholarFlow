"""Fixed, replayable translation units on the shared research run ledger."""
from copy import deepcopy
import re

from .. import source_store as sources
from ..llm import APIClient, MockLLM, get_llm
from ..research_runs import descriptor, digest
from ..source_runtime import conversation_scope
from .glossary import DEFAULT_GLOSSARY, find_terms
from .parallel import segment_units
from .prompting import system_prompt, legacy_system_prompt

MAX_UNIT=12000


def validate_inputs(value):
    fields={"text","source","target","display_mode","source_ref"}
    if not isinstance(value,dict) or set(value)!=fields:
        raise sources.SourceError("翻译输入格式无效")
    if not isinstance(value["text"],str) or not value["text"].strip() or len(value["text"])>1_000_000 or "\x00" in value["text"]:
        raise sources.SourceError("翻译原文须为1至1000000字符的文本")
    if any(not isinstance(value[k],str) or not re.fullmatch(r"[A-Za-z][A-Za-z -]{0,63}",value[k]) for k in ("source","target")):
        raise sources.SourceError("翻译语言格式无效")
    if value["display_mode"] not in ("whole","parallel"):
        raise sources.SourceError("请选择整篇或对照翻译")
    ref=value["source_ref"]
    if ref is not None and (not isinstance(ref,dict) or set(ref)!={"revision_id","locator_id"}
        or any(not isinstance(v,str) or not re.fullmatch(r"[a-f0-9]{32}",v) for v in ref.values())):
        raise sources.SourceError("固定翻译来源格式无效")
    return deepcopy(value)


def goal_description(inputs):
    # Source material is not user authority. Even quoted previews can contain
    # closing fences/newlines; keep all original text only in the fixed inputs.
    return ("翻译固定输入为"+inputs["target"]+"，保留公式、代码与引用。"
        +f"原文字符数：{len(inputs['text'])}；固定内容摘要：{digest(inputs['text'])}")


def snapshot_config(binding):
    from ..model_context_limits import context_limits
    limits=context_limits(binding)
    # Conservative UTF-8 input allowance plus output headroom. These are the
    # declared, frozen profile limits, never capabilities guessed from an ID.
    ceiling=max(1,min(MAX_UNIT,(limits["context_window_tokens"]-limits["max_output_tokens"]-2048)//2,
        limits["max_output_tokens"]//2))
    return {"translation_version":1,"translation_prompt_version":2,"glossary":deepcopy(DEFAULT_GLOSSARY),"unit_character_limit":ceiling}


def request_messages(inputs, unit, prompt_version):
    if prompt_version not in (1, 2):
        raise sources.SourceConflict("翻译提示版本不兼容；已保存结果保留，未发送新请求")
    builder = legacy_system_prompt if prompt_version == 1 else system_prompt
    return [{"role":"system","content":builder(inputs["source"],inputs["target"],unit["glossary"])},
        {"role":"user","content":unit["source"]}]


def evidence(db,state,*,writing=True):
    job=descriptor(state);inputs=job["inputs"];ref=inputs["source_ref"]
    if ref is None:return []
    scope=conversation_scope(db,state.conversation_id,writing=writing)
    position=sources.read_locator(db,ref["locator_id"],scope=scope)
    if position["revision_id"]!=ref["revision_id"]:
        raise sources.SourceNotFound("定位不属于此固定翻译来源版本")
    if position["quote"]!=inputs["text"]:
        raise sources.SourceConflict("原文与固定定位不一致，请重新载入选段或转为自由文本")
    revision=sources.read_revision(db,ref["revision_id"],scope=scope)
    return [{**position,"id":"T1","evidence_id":"T1","title":revision["title"],"version":revision["version"],
        "provenance_status":"fixed_source","semantic_verification":"not_assessed","verified":False,
        "purpose":"translation_input","conversation_id":state.conversation_id}]


def authorize(db,state):
    if descriptor(state).get("translation_version")!=1:
        raise sources.SourceConflict("翻译流程版本不兼容；原结果保留，请新建翻译")
    evidence(db,state)


def public_authorize(db,state):
    # Completed results remain readable after changing models/credentials, but
    # missing or damaged source authority cannot disclose a stored quotation.
    evidence(db,state,writing=False)


def units_for(inputs,limit=MAX_UNIT):
    parsed=segment_units(inputs["text"])
    if inputs["display_mode"]=="whole" and len(inputs["text"])<=limit and not any(u["preserve"] for u in parsed):
        return [{"index":0,"source":inputs["text"],"preserve":False}]
    result=[]
    for unit in parsed:
        if unit["preserve"]:
            result.append({**unit,"index":len(result)})
        else:
            text=unit["source"]
            while text:
                end=min(limit,len(text))
                if end<len(text):
                    boundary=max(text.rfind("\n",0,end),text.rfind("。",0,end),text.rfind(". ",0,end))
                    if boundary>=limit//2:end=boundary+1
                result.append({"index":len(result),"source":text[:end],"preserve":False})
                text=text[end:]
    return result


def _payload(context,units,offline=False):
    from ..db import SessionLocal
    state=context.state;job=descriptor(state);inputs=job["inputs"]
    with SessionLocal() as db:fixed=evidence(db,state)
    completed=sum(u.get("target") is not None for u in units)
    value={"mode":"offline" if offline else "llm","units":deepcopy(units),
        "translation":"\n\n".join(u["target"] for u in units if u.get("target") is not None) or None,
        "glossary_hits":find_terms(inputs["text"],job["glossary"]),"source_evidence":fixed,
        "provenance_status":"fixed_source" if fixed else "user_text","semantic_verification":"not_assessed",
        "model_selection":"conversation_bound","conversation_id":state.conversation_id,
        "progress":{"completed_units":completed,"total_units":len(units)},
        "partial":completed<len(units),"display_mode":inputs["display_mode"]}
    if offline:value["message"]="离线模式未生成真实译文。请配置会话模型后新建翻译；已固定原文与术语命中结果保留。"
    return value


def execute(context):
    state=context.state;job=descriptor(state);inputs=job["inputs"]
    if "translation_units" not in job:
        job["translation_units"]=[{**unit,"target":unit["source"] if unit["preserve"] else None,
            "glossary":find_terms(unit["source"],job["glossary"])} for unit in units_for(inputs,job.get("unit_character_limit",MAX_UNIT))]
        job["units_sha256"]=digest([{k:u[k] for k in ("index","source","preserve")} for u in job["translation_units"]])
        context.save()
    units=job["translation_units"]
    if digest([{k:u[k] for k in ("index","source","preserve")} for u in units])!=job["units_sha256"]:
        raise sources.SourceCorruption("翻译固定分段校验失败，未发送请求")
    client=get_llm()
    if isinstance(client,APIClient):client._authorization_check=context.authorize
    offline=isinstance(client,MockLLM)
    if offline:
        job["resume_blocked_reason"]="本次使用离线模式；请配置会话模型后新建翻译。"
        return _payload(context,units,True),False
    # Targets are a display projection. Reconstruct them only from checked
    # paid receipts, so a later cached unit cannot leak a damaged target while
    # an earlier unit is being restored or a new request is in flight.
    for unit in units:
        if not unit["preserve"]:unit["target"]=None
    for unit in units:
        context.authorize()
        if unit["preserve"]:continue
        # Existing paid keys retain exact v1 prompt bytes. An application update
        # must not turn already received units into new paid operations.
        messages=request_messages(inputs,unit,job.get("translation_prompt_version",1))
        key=f"translation:{unit['index']}:{digest(messages)}"
        restored=key in state.operations
        context.activity("restoring" if restored else "translating",f"正在恢复已保存的第{unit['index']+1}段" if restored else f"正在翻译第{unit['index']+1}/{len(units)}段；已完成段落保留")
        response=context.perform(key,"translation_unit",{"unit_index":unit["index"],"request_sha256":digest(messages)},
            lambda:client.chat(messages,temperature=.2).to_dict())
        target=response.get("content")
        if not isinstance(target,str) or not target.strip():
            job["retry_call_keys"]=[key];context.save()
            raise sources.SourceError("模型返回空译文；原文与此前段落保留，可继续重试此段")
        unit["target"]=target.strip()
        context.set_payload(_payload(context,units))
    return _payload(context,units),True


def prepare_resume(state):
    job=descriptor(state)
    if job.get("resume_blocked_reason"):
        raise sources.SourceConflict(job["resume_blocked_reason"])
    for key in job.get("retry_call_keys",[]):state.operations.pop(key,None)
    job["retry_call_keys"]=[]
