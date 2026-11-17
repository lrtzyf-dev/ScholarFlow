# -*- mode: python ; coding: utf-8 -*-
"""研序 ScholarFlow PyInstaller 打包配置（onedir，窗口程序）。

构建：
    cd backend
    .\\.venv\\Scripts\\pyinstaller.exe ..\\desktop\\scholarflow.spec --noconfirm
产物：
    backend\\dist\\ScholarFlow\\ScholarFlow.exe
"""
from PyInstaller.utils.hooks import (
    collect_all,
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)

from pathlib import Path
import os
import sys

ROOT = str(Path(SPECPATH).resolve().parent)
sys.path.insert(0, str(Path(ROOT) / 'backend'))
from app.release_manifest import verify
from app.release_info import read_json, ReleaseError

# Build from current, independently verifiable inputs. Historical bytecode
# replacement scripts are not part of this full-source build path.
_release_path = os.environ.get('SCHOLARFLOW_RELEASE_INPUTS')
if not _release_path:
    raise ReleaseError('Set SCHOLARFLOW_RELEASE_INPUTS to a freshly prepared release_tool.py output directory')
RELEASE_INPUTS = Path(_release_path).absolute()
verify(RELEASE_INPUTS, source_root=ROOT, check_environment=True)
_release_manifest = read_json(RELEASE_INPUTS / 'release-manifest.json')
if not _release_manifest['coverage'].get('frontend_provenance_verified'):
    raise ReleaseError('Frontend build provenance missing; run npm run build and prepare again')
if _release_manifest['coverage']['dependencies']['python'].get('duplicate_distributions'):
    raise ReleaseError('Build Python environment has conflicting distribution metadata; use a clean release environment')

datas = [
    (ROOT + r"\LICENSE", '.'),
    (ROOT + r"\LICENSES.md", '.'),
    (str(RELEASE_INPUTS / 'release.json'), '.'),
    (str(RELEASE_INPUTS / 'release-manifest.json'), '.'),
    (str(RELEASE_INPUTS), 'release-evidence'),
    (ROOT + r"\desktop\vendor\ripgrep\LICENSE-MIT", r"vendor\ripgrep"),
    (ROOT + r"\desktop\vendor\ripgrep\UNLICENSE", r"vendor\ripgrep"),
    (ROOT + r"\desktop\vendor\ripgrep\COPYING", r"vendor\ripgrep"),
    (ROOT + r"\desktop\vendor\ripgrep\manifest.json", r"vendor\ripgrep"),
    (ROOT + r"\desktop\vendor\terminal-licenses", r"third-party-licenses\terminal"),
    (ROOT + r"\frontend\dist", "frontend"),
    (ROOT + r"\desktop\assets\icon.ico", "assets"),
    (ROOT + r"\backend\app\samples", r"app\samples"),
    (ROOT + r"\backend\app\skills", r"app\skills"),
]
datas += collect_data_files("pdfminer")
if (RELEASE_INPUTS / 'release-signature.json').exists():
    datas.append((str(RELEASE_INPUTS / 'release-signature.json'), '.'))
datas += collect_data_files("webview")
datas += collect_data_files('winpty', includes=['*.exe'])

# 会话资料的 DOCX / XLSX 解析在函数内导入；显式保留模板与依赖许可元数据。
# PyPI 分发名 python-docx 与导入名 docx 不同，元数据使用分发名收集。
for _package in ("docx", "openpyxl"):
    datas += collect_data_files(_package)
for _distribution in ("python-docx", "openpyxl"):
    datas += copy_metadata(_distribution, recursive=True)

# OCR / 视觉栈：均为函数内惰性导入且 PyInstaller 无内置 hook，需显式收集。
# rapidocr 自带 ONNX 模型与 config.yaml；pypdfium2_raw 自带 pdfium.dll；
# onnxruntime 自带 onnxruntime.dll；cv2 自带 cv2.pyd 与配置。
_ocr_packages = ["rapidocr_onnxruntime", "pypdfium2", "pypdfium2_raw", "onnxruntime"]
ocr_binaries, ocr_datas, ocr_hidden = [], [], []
for _pkg in _ocr_packages:
    _d, _b, _h = collect_all(_pkg)
    ocr_binaries += _b
    ocr_datas += _d
    ocr_hidden += _h
ocr_datas += collect_data_files("cv2")
ocr_hidden += collect_submodules("cv2")
datas += ocr_datas

hiddenimports = []
hiddenimports += ocr_hidden
for _package in ("docx", "openpyxl", "et_xmlfile"):
    hiddenimports += collect_submodules(_package)
hiddenimports += collect_submodules("uvicorn")
hiddenimports += collect_submodules("pdfminer")
hiddenimports += collect_submodules("pdfplumber")
# MCP 客户端生态（不收集 mcp.cli，其依赖 typer 且应用不需要）
hiddenimports += ["mcp"]
hiddenimports += collect_submodules("mcp.client")
hiddenimports += collect_submodules("mcp.server")
hiddenimports += collect_submodules("mcp.shared")
hiddenimports += collect_submodules("mcp.types")
hiddenimports += collect_submodules("mcp.os")
hiddenimports += collect_submodules("mcp_types")
hiddenimports += [
    "webview",
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
    "clr_loader",
    "anyio",
    "h11",
    "httpx2",
    "httpcore2",
    "jwt",
    "sse_starlette",
    "truststore",
    "win32",
    "win32com",
]

a = Analysis(
    [ROOT + r"\desktop\launcher.py"],
    pathex=[ROOT + r"\backend", ROOT + r"\desktop"],
    binaries=[(ROOT + r"\desktop\vendor\ripgrep\rg.exe", r"vendor\ripgrep"), *collect_dynamic_libs('winpty'), *ocr_binaries],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "pytest", "reportlab", "pip", "setuptools", "PyInstaller",
        # 未使用的云 SDK / 残留依赖（pydantic-settings 仅在 AWS 配置源里惰性导入）
        "boto3", "botocore", "s3transfer", "litellm",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ScholarFlow",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=ROOT + r"\desktop\assets\app-icon.ico",
    version=str(RELEASE_INPUTS / 'windows-version.txt'),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ScholarFlow",
)
