"""Private updater probe: initialize only a disposable, credential-free database.

Dispatch this before desktop/UI/config imports. Never serve requests, resume jobs,
load user configuration or contact a model from an installation health check.
"""
from __future__ import annotations

import argparse
import re
import sys
import types
from pathlib import Path

from .atomic_files import atomic_write_json
from .release_files import no_link
from .release_info import definition, ReleaseError, read_json, resource_root
from .release_schema import schema_snapshot


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--nonce", required=True)
    args = parser.parse_args(argv)
    stage, receipt = no_link(args.stage), no_link(args.receipt)
    if (not stage.is_dir() or receipt.exists() or receipt.parent != stage.parent
            or not re.fullmatch("[a-f0-9]{64}", args.nonce)):
        raise ReleaseError("升级健康检查仅允许独立临时目录及新回执")
    authority = read_json(no_link(stage.parent / "health-authority.json"), 4096)
    if authority != {"format": "scholarflow-health-authority", "version": 1, "nonce": args.nonce,
            "stage": stage.name, "receipt": receipt.name}:
        raise ReleaseError("升级健康检查缺少本次独立目录授权")
    if "app.config" in sys.modules or "app.db" in sys.modules:
        raise ReleaseError("升级健康检查必须在配置及数据库初始化前运行")
    # Actual embedded identity, never an expected ID copied from authority/argv.
    from .release_manifest import validate_manifest
    embedded = validate_manifest(read_json(no_link(resource_root() / "release-manifest.json")))
    value = definition()
    if any(embedded.get(key) != value[key] for key in ("product_id", "version", "channel", "build_number", "data_contract")):
        raise ReleaseError("实际内嵌构建与版本定义不一致")
    config = types.ModuleType("app.config")
    config.DATA_DIR = stage
    config.DB_PATH = stage / "scholarflow.db"
    sys.modules["app.config"] = config
    from .db import init_db, engine
    try:
        init_db()
    finally:
        engine.dispose()
    atomic_write_json(receipt, {
        "format": "scholarflow-install-health", "version": 1, "nonce": args.nonce,
        "definition": value, "schema_sha256": schema_snapshot(config.DB_PATH)["sha256"],
        "input_build_id": embedded["build_id"],
        "scope": "disposable_database_only", "model_requests": 0,
    })
    return 0
