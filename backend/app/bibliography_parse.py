"""Bounded BibTeX/RIS parsing. Text remains data and raw entries are retained."""
from __future__ import annotations
import hashlib
import re
from urllib.parse import unquote, urlsplit


class BibliographyError(ValueError): pass


def normalize_doi(value):
    if not isinstance(value,str): return None
    value=value.strip()
    if value.lower().startswith(("http://","https://")):
        try:url=urlsplit(value)
        except ValueError:return None
        if (url.hostname or "").lower() not in {"doi.org","dx.doi.org"}: return None
        value=unquote(url.path.lstrip('/'))
    value=re.sub(r"^doi\s*:\s*","",value,flags=re.I).strip()
    return value.casefold() if re.fullmatch(r"10\.\d{4,9}/[^\s\"<>\x00-\x1f]+",value,re.I) and len(value)<=1024 else None


def _clean(value):
    # Preserve LaTeX spelling rather than claiming lossy transliteration.
    return re.sub(r"\s+"," ",str(value or "")).strip()


def metadata(fields, *, key="", kind="article"):
    title=_clean(fields.get("title"))
    if not title: raise BibliographyError("条目缺少标题，不能创建未命名书目")
    if len(title)>512: raise BibliographyError("标题超过512字符")
    authors=fields.get("authors",[])
    if isinstance(authors,str): authors=[_clean(a) for a in re.split(r"\s+and\s+",authors,flags=re.I)]
    if not isinstance(authors,list) or any(not isinstance(a,str) for a in authors): raise BibliographyError("作者字段无效")
    authors=[_clean(a) for a in authors if _clean(a)]
    author_text="; ".join(authors)
    if len(author_text)>1024: raise BibliographyError("作者字段超过1024字符，原条目保留供人工整理")
    raw_doi=fields.get("doi","")
    doi=normalize_doi(raw_doi) if raw_doi else None
    if raw_doi and not doi: raise BibliographyError("DOI格式无效，请校正原条目")
    year_match=re.search(r"\b(\d{4})\b",str(fields.get("year", "")))
    year=year_match.group(1) if year_match else ""
    if fields.get("year") and not year: raise BibliographyError("年份无法识别，原字段保留供校正")
    return {"title":title,"authors":author_text,"author_names":authors,"published_at":year or None,
        "doi":doi,"abstract":str(fields.get("abstract", ""))[:100000],"url":str(fields.get("url", ""))[:2048],
        "entry_key":key,"entry_type":kind,"fulltext_status":"not_acquired"}


def _split_top(text,separator):
    items=[];start=0;depth=0;quoted=False;escaped=False
    for index,c in enumerate(text):
        if escaped: escaped=False;continue
        if c=='\\': escaped=True;continue
        if c=='"' and depth==0: quoted=not quoted
        if not quoted:
            if c=='{': depth+=1
            elif c=='}': depth-=1
        if c==separator and not quoted and depth==0:
            items.append(text[start:index]);start=index+1
        if depth<0: raise BibliographyError("花括号不匹配")
    if depth or quoted: raise BibliographyError("条目的括号或引号未闭合")
    items.append(text[start:]);return items


def _value(text,macros):
    pieces=[]
    for part in _split_top(text,'#'):
        part=part.strip()
        if part.startswith('{') and part.endswith('}'): pieces.append(part[1:-1])
        elif part.startswith('"') and part.endswith('"'): pieces.append(part[1:-1])
        elif re.fullmatch(r"\d+",part): pieces.append(part)
        elif part.casefold() in macros: pieces.append(macros[part.casefold()])
        else: raise BibliographyError(f"未定义的BibTeX字符串：{part[:80]}")
    return ''.join(pieces)


def _bib_entries(text):
    """Balanced entries, including adjacent records and @ text inside fields."""
    pattern=re.compile(r"@([A-Za-z]+)\s*([({])")
    cursor=0
    while match:=pattern.search(text,cursor):
        if '%' in text[text.rfind('\n',0,match.start())+1:match.start()]:
            newline=text.find('\n',match.end());cursor=len(text) if newline<0 else newline+1;continue
        start=match.start(); opener=match[2]; depth=1; braces=0; quoted=False; escaped=False; end=None;comment=False
        for index in range(match.end(),len(text)):
            c=text[index]
            if comment:
                if c=='\n':comment=False
                continue
            if escaped: escaped=False;continue
            if c=='\\': escaped=True;continue
            if c=='"' and braces==0: quoted=not quoted
            if quoted: continue
            if c=='%' and ((opener=='{' and depth==1) or (opener=='(' and braces==0)):
                comment=True;continue
            if opener=='{':
                if c=='{': depth+=1
                elif c=='}': depth-=1
            else:
                if c=='{': braces+=1
                elif c=='}': braces-=1
                elif not braces and c==')': depth-=1
            if depth==0: end=index+1;break
        if end is None:
            # Recover at an explicit next record line; report the damaged raw
            # record instead of silently swallowing all later valid records.
            recovery=re.search(r"(?m)^\s*@([A-Za-z]+)\s*([({])",text[match.end():])
            end=match.end()+recovery.start() if recovery else len(text)
        yield match[1].lower(),opener,text[start:end].strip()
        cursor=max(end,match.end())


def _without_bib_comments(text):
    result=[];depth=0;quoted=False;escaped=False;comment=False
    for c in text:
        if comment:
            if c=='\n':comment=False;result.append(c)
            continue
        if escaped:result.append(c);escaped=False;continue
        if c=='\\':result.append(c);escaped=True;continue
        if c=='"' and depth==0:quoted=not quoted
        if not quoted:
            if c=='{':depth+=1
            elif c=='}':depth-=1
            elif c=='%' and depth==0:comment=True;continue
        result.append(c)
    return ''.join(result)


def parse_bibtex(text):
    results=[];macros={m:m.title() for m in ("jan","feb","mar","apr","may","jun","jul","aug","sep","oct","nov","dec")}
    for kind,opener,raw in _bib_entries(text):
        if kind in {"comment","preamble"}: continue
        try:
            closer='}' if opener=='{' else ')'
            body=raw[raw.index(opener)+1:].strip()
            if not body.endswith(closer): raise BibliographyError("条目结束括号缺失")
            body=_without_bib_comments(body[:-1])
            parts=_split_top(body,',')
            if kind=='string':
                for part in parts:
                    if not part.strip(): continue
                    k,separator,v=part.partition('=')
                    if not separator: raise BibliographyError("字符串定义无效")
                    macros[k.strip().lower()]=_value(v,macros)
                continue
            key=parts.pop(0).strip()
            if not key: raise BibliographyError("条目编号为空")
            fields={}
            for part in parts:
                if not part.strip(): continue
                k,separator,v=part.partition('=');k=k.strip().casefold()
                if not separator or not re.fullmatch(r"[a-z][a-z0-9_-]*",k): raise BibliographyError("字段格式无效")
                if k in fields: raise BibliographyError(f"重复字段：{k}")
                fields[k]=_value(v,macros)
            if fields.get('crossref') and not fields.get('title'):
                raise BibliographyError("依赖crossref的条目没有展开标题，请导出完整字段后重试")
            normalized=metadata({**fields,"authors":fields.get('author',[])},key=key,kind=kind)
            results.append({"raw":raw,"metadata":normalized,"fields":fields})
        except BibliographyError as exc: results.append({"raw":raw,"error":str(exc)})
    return results or [{"raw":text,"error":"仅包含注释或字符串定义，没有文献条目"}]


def parse_ris(text):
    starts=list(re.finditer(r"(?m)^TY  -\s*",text))
    if not starts: return [{"raw":text,"error":"RIS缺少TY起始标记"}]
    results=[]
    for index,start in enumerate(starts):
        raw=text[start.start():starts[index+1].start() if index+1<len(starts) else len(text)].strip()
        try:
            tags={};last=None;closed=False
            for line in raw.splitlines():
                match=re.match(r"^([A-Z0-9]{2})  - ?(.*)$",line)
                if match:
                    name,value=match.groups()
                    if name=='ER': closed=True;last=None;continue
                    if closed: raise BibliographyError("ER之后仍有字段")
                    tags.setdefault(name,[]).append(value);last=name
                elif line.strip() and last: tags[last][-1]+='\n'+line.strip()
                elif line.strip(): raise BibliographyError("RIS字段格式无效")
            if not closed: raise BibliographyError("RIS条目缺少ER结束标记")
            first=lambda *names:next((tags[n][0] for n in names if tags.get(n)),"")
            value=metadata({"title":first("TI","T1","CT"),"authors":tags.get("AU",tags.get("A1",[])),
                "year":first("PY","Y1","DA"),"doi":first("DO"),"abstract":first("AB","N2"),"url":first("UR")},
                key=first("ID"),kind=first("TY"))
            results.append({"raw":raw,"metadata":value,"fields":tags})
        except BibliographyError as exc: results.append({"raw":raw,"error":str(exc)})
    return results


def parse(format,text):
    if not isinstance(text,str) or not text.strip() or len(text.encode('utf-8'))>4*1024*1024 or '\x00' in text:
        raise BibliographyError("书目文件须为不超过4MiB的UTF-8文本")
    if format=='bibtex': rows=parse_bibtex(text)
    elif format=='ris': rows=parse_ris(text)
    elif format=='doi':
        rows=[{"raw":line,"doi":normalize_doi(line),**({} if normalize_doi(line) else {"error":"DOI格式无效"})} for line in text.splitlines() if line.strip()]
    else: raise BibliographyError("不支持的书目格式")
    if len(rows)>500: raise BibliographyError("每批最多500条书目，请分批导入")
    return rows
