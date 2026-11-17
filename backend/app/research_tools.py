"""Agent research assets. Scope comes only from the executor's current binding.

The dispatcher must apply its normal write approval and plan-mode policy to
WRITE_TOOLS. Human verification remains a separate, explicit UI operation.
"""
from copy import deepcopy
from sqlalchemy import select, text
from .db import SessionLocal
from .source_runtime import conversation_scope
from .source_store import SourceError
from . import research_store as store
from .research_models import ResearchMatrix, MatrixRevision
from .api.research import MatrixInput

READ_TOOLS = {"list_evidence_matrices", "read_evidence_matrix", "read_research_child"}
# Retain the legacy name for historical checkpoint dispatch only. It is not
# advertised and the explicit migration result below never starts retrieval.
WRITE_TOOLS = {"create_evidence_matrix", "update_evidence_matrix", "extract_evidence_matrix",
    "start_research_child", "resume_research_child"}
_field = {"type":"object","additionalProperties":False,"properties":{
    "key":{"type":"string"},"label":{"type":"string"},"query":{"type":"string"}},"required":["key","label","query"]}


def _spec(name, description, properties, required):
    return {"type":"function","function":{"name":name,"description":description,"parameters":{
        "type":"object","additionalProperties":False,"properties":properties,"required":required}}}


TOOL_SPECS = [
    _spec("start_research_child", "在当前任务内同步执行可恢复研究任务；必须复用同一request_id重试同一输入。创建不是完成，须依据真实outcome及结果交付。matrix输入为matrix_id/expected_version/field_keys(null或数组)/paper_ids(null或数组)；framework为idea/max_sources；translation为text/source/target/display_mode(whole或parallel)/source_ref(null或revision_id+locator_id)；quality为paper_id。固定来源和已知调用保留，未知付费调用需用户单独批准。", {
        "request_id":{"type":"string"},"kind":{"type":"string","enum":["matrix","framework","translation","quality"]},
        "inputs":{"type":"object"}},["request_id","kind","inputs"]),
    _spec("resume_research_child", "继续当前父任务的研究子任务，仅补未完成结果；先读取真实revision。未知付费调用只能使用用户已批准的精确许可，模型不能自行批准。矩阵存在人工修改冲突时，可指定expected_matrix_version显式合并已保存候选（不重新收费），保留人工主结论。", {
        "child_run_id":{"type":"string"},"request_id":{"type":"string"},
        "expected_revision":{"type":"integer","minimum":1},"grant_id":{"type":"string"},
        "expected_matrix_version":{"type":"integer","minimum":1}},["child_run_id","request_id","expected_revision"]),
    _spec("read_research_child", "读取当前父任务的持久研究子任务状态、已保存成果、固定引用和缺项，不恢复执行也不发起付费调用。", {
        "child_run_id":{"type":"string"},"offset":{"type":"integer","minimum":0},
        "page_size":{"type":"integer","minimum":1000,"maximum":24000},
        "evidence_offset":{"type":"integer","minimum":0},
        "evidence_limit":{"type":"integer","minimum":1,"maximum":20}},["child_run_id"]),
    _spec("list_evidence_matrices", "列出当前会话的持久证据矩阵。不会读取其他会话。", {"offset":{"type":"integer","minimum":0}}, []),
    _spec("read_evidence_matrix", "读取当前会话矩阵的固定来源、字段、提取结果和人工修订。默认只读5份资料和4个字段，使用offset继续读取。未找到不是否定结论。", {
        "matrix_id":{"type":"string"},"offset":{"type":"integer","minimum":0},
        "limit":{"type":"integer","minimum":1,"maximum":20},"field_keys":{"type":"array","items":{"type":"string"},"maxItems":10}},["matrix_id"]),
    _spec("create_evidence_matrix", "将已选文献建立为持久矩阵并固定原件版本。不会自动提取或标为已核验；检索候选须使用可恢复的矩阵任务入口，未提取的格子必须明确保留。不得静默移除无法解析的来源。", {
        "request_id":{"type":"string","description":"8-96位字母数字/下划线/连字符；重试同次创建时必须复用"},
        "title":{"type":"string"},"paper_ids":{"type":"array","items":{"type":"string"},"minItems":2,"maxItems":200},
        "fields":{"type":"array","items":_field,"minItems":1,"maxItems":40}},["request_id","title","paper_ids","fields"]),
    _spec("update_evidence_matrix", "更新矩阵标题、完整来源清单和字段，需提供当前版本。相同来源和字段保留人工校正；改变字段含义会将新单元格标为未处理，旧内容保留在修订。", {
        "matrix_id":{"type":"string"},"expected_version":{"type":"integer","minimum":1},
        "title":{"type":"string"},"paper_ids":{"type":"array","items":{"type":"string"},"minItems":2,"maxItems":200},
        "fields":{"type":"array","items":_field,"minItems":1,"maxItems":40}},["matrix_id","expected_version","title","paper_ids","fields"]),
]


def _summary(item):
    return {"matrix_id":item.id,"title":item.title,"version":item.version,"schema_version":item.document["schema_version"],
        "paper_count":len(item.document["papers"]),"fields":item.document["dimensions"],
        "status":"saved","semantic_verification":"not_performed"}


def execute(name, args, *, conversation_id):
    if name not in READ_TOOLS | WRITE_TOOLS:
        raise SourceError("未知研究资产工具")
    if not conversation_id:
        raise SourceError("此工具需要绑定真实会话，不能猜测来源范围")
    from . import research_children
    if name in research_children.READ_TOOLS | research_children.WRITE_TOOLS:
        from .runtime_context import current_runtime_config
        binding = current_runtime_config() or {}
        if binding.get("conversation_id") != conversation_id:
            raise SourceError("研究子任务会话与父绑定不一致")
        return research_children.execute(name, args, binding=binding)
    if name == "extract_evidence_matrix":
        with SessionLocal() as db:
            conversation_scope(db, conversation_id)
        return {"code": "migration_required", "not_executed": True, "new_route": "/api/research/matrix-runs",
            "message": "同步矩阵提取已迁移为固定来源可恢复任务；本次未检索或调用模型。已有矩阵和人工结论保留。"}
    permitted = set(next(s for s in TOOL_SPECS if s["function"]["name"] == name)["function"]["parameters"]["properties"])
    if not isinstance(args, dict) or set(args) - permitted:
        raise SourceError("工具参数包含未授权字段")
    with SessionLocal() as db:
        conversation_scope(db, conversation_id, writing=name in WRITE_TOOLS)
        if name == "list_evidence_matrices":
            offset=args.get("offset",0)
            if type(offset) is not int or offset<0:
                raise SourceError("矩阵分页范围无效")
            items=list(db.scalars(select(ResearchMatrix).where(ResearchMatrix.conversation_id == conversation_id)
                .order_by(ResearchMatrix.created_at,ResearchMatrix.id).offset(offset).limit(101)))
            return {"items":[_summary(item) for item in items[:100]],"next_offset":offset+100 if len(items)>100 else None}
        if name == "read_evidence_matrix":
            item=store.matrix(db,args.get("matrix_id"),conversation_id)
            offset,limit=args.get("offset",0),args.get("limit",5)
            if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 20:
                raise SourceError("矩阵分页范围无效")
            keys=args.get("field_keys") or [f["key"] for f in item.document["dimensions"][:4]]
            if not isinstance(keys,list) or len(keys)>10 or any(not isinstance(k,str) for k in keys):
                raise SourceError("请选择不超过10个字段")
            if not set(keys)<={f["key"] for f in item.document["dimensions"]}:
                raise SourceError("字段不属于当前矩阵")
            papers=item.document["papers"][offset:offset+limit]
            ids={p["id"] for p in papers}
            rows=[{**r,"cells":{pid:deepcopy(cell) for pid,cell in r["cells"].items() if pid in ids}} for r in item.document["rows"] if r["key"] in keys]
            return {**_summary(item),"papers":papers,"rows":rows,"next_offset":offset+limit if offset+limit<len(item.document["papers"]) else None}
        req=MatrixInput(conversation_id=conversation_id,**{k:v for k,v in args.items() if k!="matrix_id"})
        db.rollback()
        db.execute(text("BEGIN IMMEDIATE"))
        conversation_scope(db,conversation_id)
        if name == "create_evidence_matrix":
            if not req.request_id:
                raise SourceError("请提供可复用的创建请求编号")
            item=store.create_matrix(db,conversation_id,req.title,req.paper_ids,[f.model_dump() for f in req.fields],req.request_id,actor="agent")
        else:
            item=store.matrix(db,args.get("matrix_id"),conversation_id,writing=True)
            if req.expected_version is None or req.expected_version!=item.version:
                raise SourceError("矩阵版本已变化，请重新读取后合并")
            document=store.matrix_document(db,conversation_id,req.paper_ids,[f.model_dump() for f in req.fields],previous=item.document)
            item=store.save_matrix(db,item,document,req.expected_version,"schema",title=req.title,actor="agent")
        db.commit()
        return _summary(item)
