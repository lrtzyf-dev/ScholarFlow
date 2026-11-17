"""Local checks for live-smoke assertions; no external requests or credentials."""
import pytest

from scripts.verify_live_tools import fetch_fixture_checks, ocr_fixture_checks


@pytest.mark.parametrize("text", [
    "ScholarFlow OCR test\nSample count: 42\nAccuracy: 97%",
    "<｜ref｜>ScholarFlow OCR test<｜/ref｜><｜box｜>[[1,2,3,4]]<｜/box｜>\n"
    "<｜ref｜>Sample count: 42<｜/ref｜><｜box｜>[[5,6,7,8]]<｜/box｜>\n"
    "<｜ref｜>Accuracy: 97%<｜/ref｜>",
    "# ScholarFlow OCR test\n**Sample count: 42**\n**Accuracy: 97%**",
])
def test_ocr_requires_expected_semantic_text(text):
    assert all(ocr_fixture_checks(text).values())


@pytest.mark.parametrize("text", [
    "142 / 197",
    "<｜box｜>[[42,97,43,98]]<｜/box｜>",
    "ScholarFlow OCR test\nSample count: 142\nAccuracy: 197%",
    "ScholarFlow OCR test\nSample count: 42\nAccuracy: 97",
    "Sample count: 42\nAccuracy: 97%",
    "",
])
def test_ocr_rejects_coordinate_and_substring_false_positives(text):
    assert not all(ocr_fixture_checks(text).values())


def test_fetch_title_does_not_have_to_be_duplicated_in_body():
    checks = fetch_fixture_checks("Example Domain", "This domain is for use in documentation examples.", "example")
    assert all(checks.values())


def test_fetch_body_and_title_both_required_for_fixed_fixture():
    assert not all(fetch_fixture_checks("Example Domain", "", "example").values())
    assert not all(fetch_fixture_checks("Access denied", "", "official_docs").values())


def test_official_docs_fixture():
    assert all(fetch_fixture_checks("Your First API Call | DeepSeek API Docs", "The DeepSeek API uses an API format", "official_docs").values())
