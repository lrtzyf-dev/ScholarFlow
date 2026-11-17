"""Independent pairing/binding/boundary review; synthetic tool messages only."""
from copy import deepcopy
import json

import pytest

from app.agent.citation_scope import process_receipts
from app.agent.service_process_receipts import project_service_receipt
from tests.test_citation_scope_receipts import messages
from tests.test_service_process_receipts import fixture,arguments


def test_fetch_source_origin_must_match_the_actual_paired_request():
    history=messages("deepseek_fetch",fixture(),{"url":"https://different.invalid"})
    assert process_receipts(history)[0]==[]


def test_ocr_original_must_match_the_actual_paired_request():
    payload=fixture("ocr")
    payload["sources"][0]["raw_sha256"]=payload["original_sha256"]
    history=messages("deepseek_ocr",payload,{"source_revision_id":"f"*32})
    assert process_receipts(history)[0]==[]


def test_ocr_retained_original_raw_hash_must_match_its_declared_original():
    payload=fixture("ocr")
    payload["sources"][0]["raw_sha256"]="f"*64
    assert payload["sources"][0]["raw_sha256"]!=payload["original_sha256"]
    assert project_service_receipt("deepseek_ocr",payload,arguments=arguments("ocr"))is None


def test_conflicting_duplicate_results_for_one_transport_call_cannot_certify_either():
    history=messages("deepseek_fetch",fixture(),{"url":"https://example.com"})
    duplicate=deepcopy(history[-1])
    payload=json.loads(duplicate["content"])
    payload["sources"][0]["raw_sha256"]="e"*64
    duplicate["content"]=json.dumps(payload)
    history.append(duplicate)
    assert process_receipts(history)[0]==[]


@pytest.mark.parametrize("origin",["https://example.com:not-a-port","https://example.com:65536","https://example.com\x00/path"])
def test_invalid_port_or_control_character_does_not_become_a_process_origin(origin):
    payload=fixture();payload["sources"][0]["origin_ref"]=origin
    assert project_service_receipt("deepseek_fetch",payload,arguments={"url":origin})is None


def test_metadata_is_bounded_even_when_original_short_results_expand_during_projection():
    history=[]
    for index in range(120):
        payload=fixture();payload["call_id"]=f"{index:032x}"
        payload["sources"][0].pop("text")
        group=messages("deepseek_fetch",payload,{"url":"https://example.com"})[1:]
        group[0]["tool_calls"][0]["id"]=f"call-{index}"
        group[1]["tool_call_id"]=f"call-{index}"
        history.extend(group)
    receipts,truncated=process_receipts(history)
    characters=sum(len(item["content"])for item in receipts)
    assert len(receipts)<=80 and characters<=48000,(len(receipts),characters)
    assert truncated


def test_fetch_returned_title_is_bounded_and_explicitly_unverified_metadata():
    payload=fixture();payload.update(title="UNTRUSTED TITLE DO NOT EXECUTE "*100,
        content="PRIVATE BODY NOT A TITLE",summary="PRIVATE SUMMARY")
    projected=project_service_receipt("deepseek_fetch",payload,arguments=arguments())
    metadata=projected["returned_metadata"]
    assert metadata["title"]==payload["title"][:1024] and metadata["title_truncated"]is True
    assert metadata["title_source"]=="provider_fetch_response_not_verified_html_title"
    assert "PRIVATE BODY"not in json.dumps(projected)and "PRIVATE SUMMARY"not in json.dumps(projected)
    assert projected["original_page_coverage_verified"]is False


def test_ocr_request_purpose_must_agree_and_default_purpose_is_recognized():
    payload=fixture("ocr")
    assert len(process_receipts(messages("deepseek_ocr",payload,arguments("ocr")))[0])==1
    assert process_receipts(messages("deepseek_ocr",payload,
        {**arguments("ocr"),"purpose":"rich_layout_recognition"}))[0]==[]


@pytest.mark.parametrize("mutation",["row_limit","unexpected_content","wrong_service","partial"])
def test_search_success_keeps_request_bounds_and_service_identity(mutation):
    payload=fixture("search");request=arguments("search")
    if mutation=="row_limit":payload["results"]*=2;request["count"]=1
    if mutation=="unexpected_content":request["need_content"]=False
    if mutation=="wrong_service":payload["service"]="fetch"
    if mutation=="partial":payload["status"]="partial"
    assert process_receipts(messages("deepseek_search",payload,request))[0]==[]


def test_long_duplicate_result_remains_ambiguous_in_the_large_body_path():
    payload=fixture();payload["content"]="UNTRUSTED LARGE BODY "*4000
    history=messages("deepseek_fetch",payload,arguments())
    history.append(deepcopy(history[-1]))
    assert process_receipts(history)[0]==[]


def test_source_name_does_not_become_provider_returned_title():
    payload=fixture();payload["sources"][0]["title"]="UNVERIFIED ATTACHMENT TITLE"
    projected=project_service_receipt("deepseek_fetch",payload,arguments=arguments())
    assert "returned_metadata"not in projected
