"""Fixed-origin metadata requests; never follow imported links or log credentials."""
from __future__ import annotations
import json
from urllib.parse import quote
import httpx
from .bibliography_parse import BibliographyError, metadata, normalize_doi


class ConnectorError(BibliographyError):
    def __init__(self,message,*,invalid_credential=False):
        super().__init__(message);self.invalid_credential=invalid_credential


def get_json(url,*,headers=None,params=None,max_bytes=4*1024*1024):
    # Callers supply only fixed official HTTPS origins and server-built paths.
    try:
        with httpx.Client(timeout=httpx.Timeout(25,connect=8),follow_redirects=False,
                headers={"User-Agent":"ScholarFlow bibliographic importer",**(headers or {})}) as client:
            with client.stream('GET',url,params=params) as response:
                if response.status_code in (401,403): raise ConnectorError("访问授权不可用或权限不足，请检查连接凭据",invalid_credential=True)
                if response.status_code==404: raise ConnectorError("服务未找到该条目或资料集")
                if response.status_code==429: raise ConnectorError("服务暂时限流，请稍后重试；已保存的导入记录不受影响")
                if response.status_code!=200: raise ConnectorError(f"元数据服务返回HTTP {response.status_code}，未提交导入")
                body=bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body)>max_bytes: raise ConnectorError("元数据响应过大，请缩小资料集")
                try: value=json.loads(body)
                except (ValueError,UnicodeError): raise ConnectorError("元数据服务未返回有效JSON") from None
                return value,dict(response.headers)
    except (httpx.HTTPError,OSError):
        raise ConnectorError("元数据服务连接失败，可重试；未记录或显示请求凭据") from None


def resolve_doi(doi):
    doi=normalize_doi(doi)
    if not doi: raise BibliographyError("DOI格式无效")
    data,_=get_json('https://api.crossref.org/works/'+quote(doi,safe=''))
    message=data.get('message') if isinstance(data,dict) else None
    if not isinstance(message,dict): raise ConnectorError("Crossref响应缺少条目")
    returned=normalize_doi(message.get('DOI',''))
    if returned!=doi: raise ConnectorError("服务返回的DOI与请求不一致，未采用结果")
    names=[]
    authors=message.get('author') or []
    if not isinstance(authors,list): raise ConnectorError('Crossref作者字段格式无效')
    for author in authors:
        if not isinstance(author,dict): continue
        names.append(author.get('name') or ' '.join(str(author.get(k) or '') for k in ('given','family')).strip())
    dates=message.get('published') or message.get('issued') or {}
    parts=dates.get('date-parts',[[]]) if isinstance(dates,dict) else [[]]
    year=str(parts[0][0]) if parts and parts[0] else ''
    titles=message.get('title') or []
    if not isinstance(titles,list) or any(not isinstance(t,str) for t in titles): raise ConnectorError('Crossref标题字段格式无效')
    value=metadata({'title':titles[0] if titles else '', 'authors':names,'year':year,'doi':returned,
        'abstract':message.get('abstract',''),'url':message.get('URL','')},kind=message.get('type','article'))
    value['metadata_provider']='crossref'
    value['fulltext_status']='not_acquired'
    return value


def zotero_request(path,credential,params=None):
    if not path.startswith(('/users/','/keys/current')) or '..' in path or '?' in path or '#' in path:
        raise ConnectorError("连接器请求路径无效")
    return get_json('https://api.zotero.org'+path,headers={'Zotero-API-Key':credential,'Zotero-API-Version':'3'},params=params)


def verify_zotero(user_id,credential):
    data,_=zotero_request('/keys/current',credential)
    if not isinstance(data,dict) or not isinstance(data.get('access'),dict):
        raise ConnectorError("授权服务响应无效",invalid_credential=True)
    access=data['access'].get('user',{})
    if not isinstance(access,dict): raise ConnectorError("授权服务响应无效",invalid_credential=True)
    if str(data.get('userID'))!=str(user_id) or not access.get('library'):
        raise ConnectorError("密钥没有所选个人资料库的读取权限",invalid_credential=True)
    return {"library":True,"files":bool(access.get('files')),"remote_write_used":False,"protocol":"Zotero Web API v3"}


def zotero_metadata(item,user_id):
    data=item.get('data') or {}
    names=[]
    creators=data.get('creators') or []
    if not isinstance(creators,list) or any(not isinstance(a,dict) for a in creators): raise ConnectorError('Zotero作者字段格式无效')
    for author in creators:
        if author.get('creatorType') not in ('author','editor','bookAuthor'): continue
        names.append(author.get('name') or ' '.join(str(author.get(k) or '') for k in ('firstName','lastName')).strip())
    value=metadata({'title':data.get('title'),'authors':names,'year':data.get('date'),'doi':data.get('DOI'),
        'abstract':data.get('abstractNote',''),'url':data.get('url','')},key=item.get('key',''),kind=data.get('itemType',''))
    value['external_id']=f"user:{user_id}:{item['key']}"
    value['metadata_provider']='zotero'
    return value
