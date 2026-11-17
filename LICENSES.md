# 第三方开源组件与许可声明（Third-Party Notices）

研序 ScholarFlow 的项目代码采用根目录 MIT License。第三方组件继续遵循各自许可证；本文件是人工维护的概览，不代替对具体依赖版本、字体、嵌入资源及分发义务的核查。
本源码仓库不包含可分发的桌面安装包。我们感谢所有第三方项目的贡献者。

2026-11-15 发现的待解决事项：`proxy-tools 0.1.0`（pywebview 的传递依赖）仅提供源码包；
元数据标注 MIT，而模块头声明 BSD 并引用源码包内缺失的 LICENSE。已记录来源哈希和两次
相同哈希的本地 wheel 构建，尚需上游澄清或经过独立审查的替代实现，不能把该组件声明为许可核查已通过。

## 本地界面字体

界面随包提供 Inter 与基于 Noto Sans SC 裁剪并更名的 ScholarFlow CJK Sans，均采用
SIL Open Font License 1.1。字体来源与派生说明见 `frontend/src/theme/FONTS.md`；
许可原文位于 `frontend/public/licenses/`，构建后随桌面前端的 `licenses/` 目录分发。

## 后端（Python）

| 组件 | 许可 | 版权 / 说明 |
| --- | --- | --- |
| FastAPI | MIT | Copyright (c) Sebastián Ramírez |
| Uvicorn | BSD-3-Clause | Copyright (c) Encode |
| Starlette | BSD-3-Clause | Copyright (c) Encode |
| Pydantic | MIT | Copyright (c) Pydantic Team |
| pydantic-settings | MIT | Copyright (c) Pydantic Team |
| SQLAlchemy | MIT | Copyright (c) 2005-2026 SQLAlchemy Authors |
| NumPy | BSD-3-Clause | Copyright (c) NumPy Developers |
| httpx | BSD-3-Clause | Copyright (c) Encode |
| httpcore | BSD-3-Clause | Copyright (c) Encode |
| anyio | MIT | Copyright (c) Alex Grönholm |
| h11 | MIT | Copyright (c) Nathaniel J. Smith |
| click | BSD-3-Clause | Copyright (c) Pallets |
| python-multipart | Apache-2.0 | Copyright (c) Andrew Dong |
| pdfplumber | MIT | Copyright (c) Jeremy Singer-Vine |
| pdfminer.six | MIT | Copyright (c) 2014-2026 pdfminer.six contributors |
| python-docx | MIT | Copyright (c) 2013 Steve Canny；用于 DOCX 段落与表格读取 |
| openpyxl | MIT | Copyright (c) 2010 openpyxl；用于 XLSX 工作表、单元格与公式原文读取，不执行公式 |
| et-xmlfile（openpyxl 依赖） | MIT / PSF-2.0 | Copyright (c) 2010 openpyxl；包含 Python 标准库衍生代码，保留 Python Software Foundation 许可 |
| lxml（python-docx 依赖） | BSD-3-Clause | Copyright (c) 2004 Infrae；保留其随附许可与组件声明 |
| typing_extensions（python-docx 等组件依赖） | PSF-2.0 | Python Software Foundation；保留随包的完整 Python 许可 |
| Pillow | MIT-CMU | Copyright (c) 1997-2011 Secret Labs AB, 2010-2026 Fredrik Lundh / contributors |
| pywebview | BSD-3-Clause | Copyright (c) 2014-2017 Roman Sirokov |
| pythonnet（pywebview 依赖） | MIT | Copyright (c) 2006-2026 Python.NET contributors |
| MCP Python SDK（mcp） | MIT | Copyright (c) 2024-2026 Model Context Protocol contributors |
| mcp-types | MIT | Copyright (c) 2024-2026 Model Context Protocol contributors |
| httpx2 / httpcore2 | BSD-3-Clause | Copyright (c) Encode（MCP 2.x 专用传输层） |
| PyJWT | MIT | Copyright (c) 2015-2026 José Padilla |
| sse-starlette | BSD-3-Clause | Copyright (c) 2022-2026 sysid |
| truststore | MIT | Copyright (c) 2022 Seth Michael Larson |
| pywin32 | PSF-2.0 | Copyright (c) Mark Hammond（Windows 系统集成） |

DOCX / XLSX 解析依赖的完整许可证及包元数据通过桌面打包配置随程序保留在
`ScholarFlow/_internal/` 下对应的 `*.dist-info/` 目录中，包括 python-docx 的
`licenses/LICENSE`、openpyxl 的 `LICENCE.rst`、et-xmlfile 的 `LICENCE.rst` / `LICENCE.python`
及 lxml 的 `licenses/`。实际路径以构建时安装的依赖版本为准。

## 前端（npm）

| 组件 | 许可 | 说明 |
| --- | --- | --- |
| Vue 3 | MIT | Copyright (c) 2013-2026 Yuxi (Evan) You |
| Vue Router | MIT | Copyright (c) 2013-2026 Eduardo San Martin Morote |
| Element Plus | MIT | Copyright (c) 2020-2026 Element Plus contributors |
| Axios | MIT | Copyright (c) 2014-present Matt Zabriskie & contributors |
| pdf.js (pdfjs-dist) | Apache-2.0 | Copyright (c) Mozilla and individual contributors |
| Cytoscape.js | MIT | Copyright (c) 2016-2026 The Cytoscape Consortium |
| markdown-it | MIT | Copyright (c) 2014 Vitaly Puzrin, Alex Kocharin |
| Highlight.js | BSD-3-Clause | Copyright (c) 2006 Ivan Sagalaev；按需包含 11 种语言的语法 |
| KaTeX（含公式字体） | MIT | Copyright (c) 2013-2020 Khan Academy and other contributors |
| @mdit/plugin-katex、@mdit/plugin-tex、@mdit/helper | MIT | Copyright (c) 2016 Waylon Flinn；Copyright (C) 2022 / 2024 - PRESENT by MrHope |
| lucide-vue-next | ISC / MIT | Lucide Contributors，含 Cole Bemis 的 Feather 衍生部分 |
| Vite（构建工具，不随产物分发） | MIT | Copyright (c) 2019-2026 Yuxi (Evan) You |

## 图表与前端传递依赖

| 组件 | 许可 | 说明 |
| --- | --- | --- |
| Apache ECharts | Apache-2.0 | 图表与数据可视化 |
| Mermaid | MIT | 图示生成 |
| elkjs | EPL-2.0 OR GPL-3.0-or-later | 依赖的图布局组件；采用 EPL-2.0 条款时遵循该许可证 |
| DOMPurify | MPL-2.0 OR Apache-2.0 | HTML 安全清理；采用 Apache-2.0 条款时遵循该许可证 |

## 桌面搜索组件

桌面构建目录保留 ripgrep 15.2.0 的官方 Windows x86_64 MSVC 二进制，按 MIT 许可使用。
Copyright (c) 2015 Andrew Gallant；完整许可、双许可说明及来源/校验清单位于
`ScholarFlow/_internal/vendor/ripgrep/`（源码目录为 `desktop/vendor/ripgrep/`）。发布 ZIP 和 rg.exe 的 SHA256
已分别与 GitHub 发布资产摘要及本地解压结果核对，搜索不依赖用户另装 ripgrep。

## 桌面终端组件

| 组件 | 版本 | 许可证与来源 |
|------|------|--------------|
| @xterm/xterm | 6.0.0 | MIT；The xterm.js authors、SourceLair Private Company、Christopher Jeffrey；https://github.com/xtermjs/xterm.js |
| @xterm/addon-fit | 0.11.0 | MIT；The xterm.js authors；同上 |
| pywinpty | 3.1.2 | MIT；Copyright (c) 2017 Spyder IDE；https://github.com/andfoy/pywinpty |
| winpty-rs（pywinpty 原生扩展） | 随 pywinpty wheel | MIT / Apache-2.0，本包选择 MIT；https://github.com/andfoy/winpty-rs |
| conpty.dll、OpenConsole.exe（pywinpty 附带） | 随 pywinpty wheel | MIT；Microsoft Corporation；https://github.com/microsoft/terminal |
| winpty.dll、winpty-agent.exe（pywinpty 附带） | 随 pywinpty wheel | MIT；Ryan Prichard；https://github.com/rprichard/winpty |

完整许可及原生组件上游许可原文/来源/SHA256 随桌面包放在 `third-party-licenses/`；pywinpty 的运行库按其 wheel 原样随包提供，构建清单记录各二进制哈希。终端使用系统自带 PowerShell/cmd，不分发 Windows Shell 或系统字体。

## 项目演示材料

仓库附带的 `ScholarFlow-demo.pdf` 为作者编写脚本生成的虚构演示材料，不是外部论文或真实研究数据，随本项目代码按根目录 MIT License 授权。

> 说明：以上清单基于构建时的依赖版本整理；具体许可文本以各组件随附的 LICENSE 文件为准。

前端字体许可原文随 `frontend/public/licenses/` 保留；ripgrep 许可原文随 `desktop/vendor/ripgrep/` 保留。其他依赖的完整许可文本以所安装的上游包为准。特别是 `proxy-tools 0.1.0` 的上游元数据与源码头许可标记不一致且源码包缺少其引用的许可文件；该问题尚未澄清。请勿据此仓库构建或分发桌面二进制，除非先解决该依赖许可问题并完成完整的发行依赖核查。
