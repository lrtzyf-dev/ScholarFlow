"""One product version source; build integrity and publisher trust are separate."""
from __future__ import annotations
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
import sys


class ReleaseError(ValueError):
    pass


def canonical_json(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')


def unique_object(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ReleaseError('发行文件含重复字段')
        result[key]=value
    return result


def read_json(path,max_bytes=32*1024*1024):
    path=Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size>max_bytes:raise ReleaseError('发行文件缺失、链接或过大')
    try:return json.loads(path.read_text(encoding='utf-8'),object_pairs_hook=unique_object,
        parse_constant=lambda _:(_ for _ in ()).throw(ReleaseError('发行文件含无效数值')))
    except (UnicodeError,json.JSONDecodeError):raise ReleaseError('发行文件不是有效UTF-8 JSON') from None


def validate_definition(value):
    allowed={'schema_version','product_id','product_name','version','channel','build_number','data_contract','signing'}
    if not isinstance(value,dict) or set(value)!=allowed or value.get('schema_version')!=1 or value.get('product_id')!='scholarflow':
        raise ReleaseError('发行定义结构不受支持')
    if not isinstance(value.get('version'),str) or not re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)',value['version']):
        raise ReleaseError('产品版本必须为三段数字版本')
    numbers=[int(n) for n in value['version'].split('.')]
    if any(n>65535 for n in numbers) or type(value.get('build_number')) is not int or not 0<=value['build_number']<=65535:
        raise ReleaseError('Windows版本资源的四个部分必须为0至65535')
    if value.get('channel') not in ('development','candidate','stable'):raise ReleaseError('发行通道无效')
    if not isinstance(value.get('product_name'),str) or not 1<=len(value['product_name'])<=100:raise ReleaseError('产品名称无效')
    data=value.get('data_contract')
    if (not isinstance(data,dict) or set(data)!={'generation','checkpoint_protocol','rollback_policy'}
            or any(type(data.get(k)) is not int or data[k]<1 for k in ('generation','checkpoint_protocol'))
            or data.get('rollback_policy')!='exact_schema_or_verified_compatible_snapshot'):raise ReleaseError('数据兼容契约无效')
    signing=value.get('signing')
    if (not isinstance(signing,dict) or set(signing)!={'required_for_public_release','trusted_ed25519_keys'}
            or type(signing.get('required_for_public_release')) is not bool or not isinstance(signing.get('trusted_ed25519_keys'),dict)):
        raise ReleaseError('发行签名策略无效')
    return value


def resource_root():
    if getattr(sys,'frozen',False):return Path(getattr(sys,'_MEIPASS',Path(sys.executable).parent))
    return Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def definition():
    return validate_definition(read_json(resource_root()/'release.json',64*1024))


def public_release():
    value=definition();root=resource_root()
    result={k:value[k] for k in ('product_id','product_name','version','channel','build_number','data_contract')}
    result.update(build_id='development',source_sha256=None,build_manifest_available=False,
        publisher_signature='not_verified',distribution_state='source_checkout' if not getattr(sys,'frozen',False) else 'unidentified_bundle',
        definition_sha256=hashlib.sha256(canonical_json(value)).hexdigest())
    path=root/'release-manifest.json'
    if path.exists():
        manifest=read_json(path)
        from .release_manifest import validate_manifest
        validate_manifest(manifest)
        if not isinstance(manifest,dict) or manifest.get('definition_sha256')!=result['definition_sha256']:
            raise ReleaseError('安装构建清单与产品版本定义不一致')
        result.update(build_id=manifest.get('build_id'),source_sha256=manifest.get('source_sha256'),
            build_manifest_available=True,distribution_state=manifest.get('artifact_state','prepared_inputs'))
        # A payload's own signature claim does not establish a trusted publisher.
        # Actual verification is performed by the offline release verifier.
        result['publisher_signature']='requires_offline_verification' if (root/'release-signature.json').exists() else 'unsigned'
    return result


VERSION=definition()['version']
