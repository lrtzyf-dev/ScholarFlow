"""Complete, bounded source comparisons over verified fixed extractions.

Unique-content anchors avoid quadratic sequence matching. A repeated region
with no safe anchor is shown as a whole replacement, never silently omitted.
These are extraction differences, not a claim about rendered layout or meaning.
"""
from bisect import bisect_left
from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import io

from . import source_documents as documents, source_store as store
from .source_models import SourceExtraction


@dataclass(frozen=True)
class Unit:
    text: str
    label: str
    kind: str
    signature: str


def _unit(text, label, kind, context):
    signature=hashlib.sha256(json.dumps([kind,context,text],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    return Unit(text,label,kind,signature)


def _units(db, record, scope):
    if db.get(SourceExtraction,record["revision_id"])is None:
        if record.get("text")is None:
            raise store.SourceError("此版本还没有固定文本提取，无法比较正文；请先核对原件。")
        doc={"format":"text","blocks":[{"text":record["text"],"kind":"retained_text"}],
             "omitted_content":["尚无固定结构提取，仅比较已保存文本"],"parser":record.get("parser")}
    else:
        doc=documents.read_document(db,record["revision_id"],scope=scope)
    result=[]
    for index,block in enumerate(doc["blocks"],1):
        label=(f'第 {block["page_index"]+1} 页'if "page_index"in block else
               f'{block["sheet"]}!{block.get("cell_range","")}'if "sheet"in block else
               f'{block.get("part_label","Word")} {block.get("xml_path",index)}'if block.get("kind")=="word_part"else
               f'段落 {block["paragraph_index"]}'if "paragraph_index"in block else
               f'行 {block.get("line_start",index)}')
        # Line/paragraph numbers are display positions, not content identity:
        # inserting one line must not mark every later line as changed.
        context={key:block[key]for key in ("sheet","cell_range","part_name","section_kind","page_index")if key in block}
        # Stream lines and pack short lines in a long block. A PDF/legacy text
        # made of millions of newlines must not allocate millions of objects.
        text=block.get("text","")
        pieces=io.StringIO(text,newline="") if len(text)<=16000 else (
            text[start:start+2000]for start in range(0,len(text),2000))
        if not text:pieces=[""]
        pending="";individual_lines=(len(text)<=16000 and
                                      text.count("\n")+text.count("\r")<=500)
        for line in pieces:
            pending+=line
            if len(text)>16000 or len(pending)>=2000:
                for start in range(0,len(pending),2000):result.append(_unit(pending[start:start+2000],label,"text",context))
                pending=""
            elif individual_lines:
                result.append(_unit(pending,label,"text",context));pending=""
        if pending:result.append(_unit(pending,label,"text",context))
        metadata={key:value for key,value in block.items()if key not in (
            "text","kind","line_start","line_end","paragraph_index","block_id","xml_path","part_label")}
        if metadata:
            value=json.dumps(metadata,ensure_ascii=False,sort_keys=True,separators=(",",":"))
            for start in range(0,len(value),2000):
                result.append(_unit(value[start:start+2000],label,"structure",context))
    return result,doc


def _spans(a,b):
    """Monotonic full coverage, O(n log n) per bounded recursion level."""
    spans=[];coarse=False
    def add(kind,x,y,u,v):
        if x!=y or u!=v:spans.append((kind,x,y,u,v))
    def visit(x,y,u,v,depth=0):
        nonlocal coarse
        start=x;other=u
        while x<y and u<v and a[x]==b[u]:x+=1;u+=1
        add("equal",start,x,other,u)
        tail_y,tail_v=y,v
        while x<y and u<v and a[y-1]==b[v-1]:y-=1;v-=1
        if x==y or u==v:
            add("replace",x,y,u,v)
        elif depth>=8:
            coarse=True;add("replace",x,y,u,v)
        else:
            ca,cb=Counter(a[x:y]),Counter(b[u:v])
            positions={value:i for i,value in enumerate(b[u:v],u)if cb[value]==1 and ca[value]==1}
            pairs=[(i,positions[value])for i,value in enumerate(a[x:y],x)if value in positions]
            tails=[];nodes=[];links=[]
            for i,pair in enumerate(pairs):
                k=bisect_left(tails,pair[1]);links.append(nodes[k-1]if k else -1)
                if k==len(tails):tails.append(pair[1]);nodes.append(i)
                else:tails[k]=pair[1];nodes[k]=i
            chain=[];node=nodes[-1]if nodes else -1
            while node>=0:chain.append(pairs[node]);node=links[node]
            if not chain:
                coarse=True;add("replace",x,y,u,v)
            else:
                for i,j in reversed(chain):
                    visit(x,i,u,j,depth+1);add("equal",i,i+1,j,j+1);x,u=i+1,j+1
                visit(x,y,u,v,depth+1)
        add("equal",y,tail_y,v,tail_v)
    visit(0,len(a),0,len(b))
    return spans,coarse


def compare(db,left_id,right_id,*,scope,offset=0,limit=100,changes_only=True):
    # Both authorizations precede structure reads, preserving scope isolation.
    left=store.read_revision(db,left_id,scope=scope)
    right=store.read_revision(db,right_id,scope=scope)
    if left["source_id"]!=right["source_id"]:
        raise store.SourceError("请选择同一资料的两个固定版本进行比较")
    old,ld=_units(db,left,scope);new,rd=_units(db,right,scope)
    spans,coarse=_spans([u.signature for u in old],[u.signature for u in new])
    counts={"unchanged":0,"removed":0,"added":0};rows=[];number=0
    def emit(unit,status,old_index=None,new_index=None):
        nonlocal number
        counts[status]+=1
        if status=="unchanged"and changes_only:return
        if offset<=number<offset+limit:
            rows.append({"status":status,"text":unit.text,"label":unit.label,"kind":unit.kind,
                         "old_unit":old_index,"new_unit":new_index,"row":number})
        number+=1
    for kind,x,y,u,v in spans:
        if kind=="equal":
            for i,j in zip(range(x,y),range(u,v)):emit(old[i],"unchanged",i+1,j+1)
        else:
            for i in range(x,y):emit(old[i],"removed",i+1)
            for j in range(u,v):emit(new[j],"added",new_index=j+1)
    summary=lambda r,d:{"revision_id":r["revision_id"],"version":r["version"],"title":r["title"],
        "raw_sha256":r["raw_sha256"],"format":d["format"],"parser":d.get("parser"),"omitted_content":d.get("omitted_content",[])}
    return {"source_id":left["source_id"],"left":summary(left,ld),"right":summary(right,rd),
        "rows":rows,"offset":offset,"next_offset":offset+limit if offset+limit<number else None,
        "total_rows":number,"counts":counts,"changes_only":changes_only,"coarse_regions":coarse,
        "comparison_scope":"fixed_extraction_text_and_structure","rendered_layout_compared":False,
        "raw_bytes_identical":left["raw_sha256"]==right["raw_sha256"],"semantic_equivalence_verified":False}
