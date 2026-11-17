"""离线 lexical 审计的误报回归（Q9）：英文逐字引用在大小写/词形/分词差异下
仍应判为支持，而无关证据仍须判为不支持。"""
from app.agent.audit import _lexical_supported, _token_matches


def test_verbatim_quote_with_case_difference_is_supported():
    claim = "The model uses attention mechanisms for language understanding"
    evidence = ["THE MODEL USES ATTENTION MECHANISMS FOR LANGUAGE UNDERSTANDING."]
    assert _lexical_supported(claim, evidence) is True


def test_morphological_difference_is_supported():
    # 论断用复数/现在时，证据用单数/过去时，仅词形差异
    claim = "The models use attention mechanisms in language tasks"
    evidence = ["The model used attention mechanism in a language task."]
    assert _lexical_supported(claim, evidence) is True


def test_token_match_prefix_tolerance():
    assert _token_matches("use", "used") is True
    assert _token_matches("model", "models") is True
    assert _token_matches("talk", "talked") is True
    # 偶然前缀（公共词干太短或差距过大）不匹配
    assert _token_matches("cat", "category") is False
    assert _token_matches("a", "abandon") is False


def test_unrelated_evidence_is_not_supported():
    claim = "attention mechanisms improve language model performance"
    evidence = ["Marine biology studies ocean temperature and coral reef ecology."]
    assert _lexical_supported(claim, evidence) is False


def test_empty_claim_is_supported_but_empty_evidence_is_not():
    assert _lexical_supported("", ["some evidence text"]) is True
    assert _lexical_supported("substantive claim", []) is False
