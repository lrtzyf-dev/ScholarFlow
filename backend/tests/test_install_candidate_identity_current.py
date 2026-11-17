"""Signed synthetic candidates bind user preview, stage and health execution."""
from pathlib import Path

import pytest

from app import release_install as module
from app.release_info import ReleaseError
from tests.test_release_install import setup


def invoke(s, **options):
    return module.install(s['candidate'], s['target'], s['data'],
                          trusted_keys=s['trust'], **options)


def candidate_id(s):
    return module.verify_candidate(s['candidate'], s['trust'])['candidate_id']


def overwrite_synthetic_tree(source, destination):
    for path in source.rglob('*'):
        if path.is_file():
            (destination / path.relative_to(source)).write_bytes(path.read_bytes())


def test_expected_identity_can_commit_matching_signed_candidate(setup):
    s = setup
    expected = candidate_id(s)
    result = invoke(s, expected_candidate_id=expected)
    assert result['state'] == 'committed' and result['candidate_id'] == expected
    assert len(s['health']) == 1


def test_expected_identity_mismatch_refuses_before_stage_and_health(setup):
    s = setup
    before = module._tree_fingerprint(s['target'])
    with pytest.raises(ReleaseError, match='身份已变化'):
        invoke(s, expected_candidate_id='0' * 64)
    assert module._tree_fingerprint(s['target']) == before
    assert not s['health']
    state = s['target'].parent / '.installed-updates'
    assert not (state / 'pending.json').exists()


@pytest.mark.parametrize('expected', ['', 'A' * 64, 'x' * 64, 1, True, 'a' * 63])
def test_malformed_expected_identity_is_rejected(setup, expected):
    s = setup
    with pytest.raises(ReleaseError, match='身份无效'):
        invoke(s, expected_candidate_id=expected)
    assert not s['health']


def test_distinct_valid_signed_stage_is_rejected_before_activation(setup, monkeypatch):
    s = setup
    before = module._tree_fingerprint(s['target'])
    different = s['create']('different-signed', 3)
    original = module.shutil.copytree
    def swapped(source, destination, *args, **kwargs):
        return original(different if Path(source) == s['candidate'] else source,
                        destination, *args, **kwargs)
    monkeypatch.setattr(module.shutil, 'copytree', swapped)
    with pytest.raises(ReleaseError, match='准备期间变化'):
        invoke(s, expected_candidate_id=candidate_id(s))
    assert module._tree_fingerprint(s['target']) == before
    assert not s['health']
    assert module.recover(s['target'], s['data'], trusted_keys=s['trust'])['state'] == 'rolled_back'


@pytest.mark.parametrize('boundary', ['ready', 'candidate_activated'])
def test_valid_signed_replacement_is_not_executed_for_health(setup, monkeypatch, boundary):
    s = setup
    different = s['create']('different-before-health', 3)
    def change(name):
        if name != boundary:
            return
        if boundary == 'ready':
            state = s['target'].parent / '.installed-updates'
            destination = module._pending(state) / 'stage'
        else:
            destination = s['target']
        overwrite_synthetic_tree(different, destination)
    monkeypatch.setattr(module, '_checkpoint', change)
    with pytest.raises(ReleaseError):
        invoke(s, expected_candidate_id=candidate_id(s))
    assert not s['health']
    # An unexpected directory is retained for operator recovery, never erased.
    assert module.verify_candidate(s['target'], s['trust'])['candidate_id'] != candidate_id(s)
