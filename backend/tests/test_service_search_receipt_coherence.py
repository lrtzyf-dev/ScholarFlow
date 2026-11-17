"""Search result/source coherence, using only synthetic paired adapter shapes."""
from copy import deepcopy
import json

import httpx
import pytest

from app.agent.citation_scope import process_receipts
from tests.test_citation_scope_receipts import messages
from tests.test_service_process_receipts import fixture,arguments
from tests.test_deepseek_service_tools import binding,network,execute,item


@pytest.mark.parametrize("mutation",["empty_with_sources","extra_source","different_row_origin"])
def test_search_retained_sources_must_correspond_to_actual_returned_rows(mutation):
    payload=fixture("search");request=arguments("search")
    payload["results"]=[{"url":"https://example.com","result_type":"search_result",
        "content_saved":True,"source":deepcopy(payload["sources"][0])}]
    assert len(process_receipts(messages("deepseek_search",payload,request))[0])==1
    if mutation=="empty_with_sources":payload.update(status="empty",results=[])
    if mutation=="extra_source":
        extra=deepcopy(payload["sources"][0]);extra["revision_id"]="f"*32
        payload["sources"].append(extra)
    if mutation=="different_row_origin":payload["results"][0]["url"]="https://different.invalid"
    assert process_receipts(messages("deepseek_search",payload,request))[0]==[]


@pytest.mark.parametrize("need_content",[False,True])
def test_actual_search_adapter_projects_coherent_summary_or_retained_content(db,binding,network,need_content):
    request={"query":"synthetic source coherence","count":1,"need_content":need_content}
    network[1][0]=lambda call:httpx.Response(200,json={"results":[item("# RETAINED PRIVATE BODY"if need_content else "")]})
    result,_=execute(db,binding,"deepseek_search",request)
    receipts,_=process_receipts(messages("deepseek_search",result,request))
    assert len(network[0])==1 and len(receipts)==1
    metadata=json.loads(receipts[0]["content"])
    assert metadata["returned_result_count"]==1
    assert len(metadata["returned_sources"])==int(need_content)
    assert metadata["summary_is_source_text"]is False
    assert "RETAINED PRIVATE BODY"not in receipts[0]["content"]
    assert "synthetic search summary"not in receipts[0]["content"]
    assert not metadata["original_page_coverage_verified"]
    if need_content:
        assert metadata["returned_sources"][0]["revision_id"]==result["results"][0]["source"]["revision_id"]
        assert metadata["returned_sources"][0]["url"]==result["results"][0]["url"]
