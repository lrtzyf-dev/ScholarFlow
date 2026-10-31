# -*- mode: python ; coding: utf-8 -*-
"""研序 ScholarFlow PyInstaller 打包配置（onedir，窗口程序）。

构建：
    cd backend
    .\\.venv\\Scripts\\pyinstaller.exe ..\\desktop\\scholarflow.spec --noconfirm
产物：
    backend\\dist\\ScholarFlow\\ScholarFlow.exe
"""
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, collect_dynamic_libs

ROOT = r"D:\Desktop\研序"

datas = [
    (ROOT + r"\desktop\vendor\ripgrep\LICENSE-MIT", r"vendor\ripgrep"),
    (ROOT + r"\desktop\vendor\ripgrep\UNLICENSE", r"vendor\ripgrep"),
    (ROOT + r"\desktop\vendor\ripgrep\COPYING", r"vendor\ripgrep"),
    (ROOT + r"\desktop\vendor\ripgrep\manifest.json", r"vendor\ripgrep"),
    (ROOT + r"\frontend\dist", "frontend"),
    (ROOT + r"\desktop\assets\icon.ico", "assets"),
    (ROOT + r"\backend\app\samples", r"app\samples"),
    (ROOT + r"\backend\app\skills", r"app\skills"),
]
datas += collect_data_files("pdfminer")
datas += collect_data_files("webview")
datas += collect_data_files('winpty', includes=['*.exe'])

hiddenimports = []
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
    binaries=[(ROOT + r"\desktop\vendor\ripgrep\rg.exe", r"vendor\ripgrep"), *collect_dynamic_libs('winpty')],
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
    icon=ROOT + r"\desktop\assets\icon.ico",
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
