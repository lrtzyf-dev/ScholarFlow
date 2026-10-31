# 第三方开源组件与许可声明（Third-Party Notices）

研序 ScholarFlow 为专有软件，但其运行依赖以下宽松许可（MIT / BSD / Apache-2.0）的
开源组件。这些组件可随本软件进行商业闭源分发。我们在此向各开源项目的贡献者致谢。

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

## 桌面搜索组件

本软件随包分发 ripgrep 15.2.0 的官方 Windows x86_64 MSVC 二进制，按 MIT 许可使用。
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

## 参考项目（仅作设计参考，未直接拷贝其代码）

- **Octop**（https://github.com/TencentCloud/Octop），依据 MIT 许可发布：

  > Copyright (c) 2026 Octop
  >
  > Permission is hereby granted, free of charge, to any person obtaining a copy
  > of this software and associated documentation files (the "Software"), to deal
  > in the Software without restriction, including without limitation the rights
  > to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
  > copies of the Software, and to permit persons to whom the Software is
  > furnished to do so, subject to the following conditions:
  >
  > The above copyright notice and this permission notice shall be included in all
  > copies or substantial portions of the Software.
  >
  > THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND.

  研序 ScholarFlow 仅参考其设计令牌（design tokens）与交互理念，未复制其源代码；
  也未使用 "Octop"、"Tencent" 名称、图标或品牌素材。

## 文档与示例

- 示例 PDF 由本软件使用 ReportLab（BSD 许可）生成，内容为公有领域/自制演示文本。
- 字体：演示 PDF 嵌入 **Noto Sans SC Regular（SIL Open Font License 1.1）**，该字体允许商用、嵌入与再分发；
  构建脚本（backend/data/make_noto_static.py）从 Google 发布的 Noto Sans SC 可变字体生成静态 Regular 实例。
  软件运行时仅渲染用户提供的 PDF（其字体由原文件自带），程序本身不随附任何专有字体。
  Noto Sans SC 版权声明：© 2014-2021 Adobe (http://www.adobe.com/)，含保留字体名称 "Source"，
  以 SIL OFL 1.1 授权，完整许可证见 http://scripts.sil.org/OFL 。

> 说明：以上清单基于构建时的依赖版本整理；具体许可文本以各组件随附的 LICENSE 文件为准。

新增的 Markdown 渲染、代码高亮、公式字体和图标依赖的完整许可随桌面验收包保存在 `third-party-licenses/`。
