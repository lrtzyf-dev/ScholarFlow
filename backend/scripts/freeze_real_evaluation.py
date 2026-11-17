"""Create a code-only, hash-verified evaluation snapshot; never read credentials.

This is not a build or release. Run an opt-in evaluation script from the copied
backend using its --credentials-dir argument; that script reads the protected
key in memory and writes no private reasoning. User files, .env, config, DB,
virtualenvs and frontend outputs are deliberately outside this allowlist.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import shutil


def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()


def freeze(destination):
    root = Path(__file__).resolve().parents[2]
    paths = sorted((root / "backend/app").rglob("*.py"))
    paths += [root / "backend/scripts" / name for name in (
        "verify_real_daily_use.py", "verify_real_framework.py", "verify_real_named_default.py",
        "verify_real_acceptance.py", "review_real_acceptance.py", "verify_real_capabilities.py",
        "verify_citation_f_review.py")]
    paths += [root / "backend/eval/commercial" / name for name in ("case_manifest.py", "manifest.json")]
    # Explicit fabricated public-output fixture only; never glob user/test data.
    paths += [root / "backend/tests/fixtures/citation-f-public-drafts.json"]
    paths += [root / "release.json"]
    if destination.exists(): raise ValueError("EvaluationDestinationAlreadyExists")
    if any(p.is_symlink() or not p.is_file() for p in paths): raise ValueError("EvaluationSourceMustBeOrdinaryFiles")
    expected = {p.relative_to(root).as_posix(): digest(p) for p in paths}
    destination.mkdir(parents=True)
    for path in paths:
        target = destination / path.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    if expected != {p.relative_to(root).as_posix(): digest(p) for p in paths}:
        raise ValueError("SourceChangedWhileFreezingEvaluation")
    if expected != {name: digest(destination / name) for name in expected}:
        raise ValueError("EvaluationSnapshotChecksumMismatch")
    value = {"schema_version": 1, "scope": "code-only fixed evaluation input, not a packaged release",
        "source_sha256": expected, "credential_files_copied": 0, "user_documents_copied": 0,
        "provider_calls": 0, "limits": ["No frontend/installed binary verification", "No release approval"]}
    (destination / "evaluation-inputs.json").write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"snapshot": str(destination), "verified_files": len(paths), "provider_calls": 0}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    freeze(args.output.resolve())
