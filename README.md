# 研序 ScholarFlow

**研序 ScholarFlow** 由张祎凡独立开发，是面向个人研究者的本地科研工作台，用于整理论文、检索原文证据，并围绕文献进行带来源的问答。项目原创代码由作者独立完成，著作权归作者所有。主要界面为简体中文。

> 本仓库提供源代码和开发说明，不包含安装包、用户数据库、个人文献、聊天记录或 API 密钥。

[English overview](#english) · [功能与边界](#功能与边界) · [本地运行](#本地运行) · [上传到 GitHub](docs/github-publish.md) · [许可证](#许可证)

## 功能

- PDF 文献导入、文本解析、混合检索和版式阅读。
- 基于检索证据回答研究问题；回答中的引用可以打开相应来源位置。
- 支持兼容的大模型接口，可在应用设置中配置；本地离线演示需要显式启用。
- 文献比较、关系图、研究写作辅助，以及基于本地项目文件的工作流。
- 提供 Windows 桌面运行方式，也可在本机浏览器中启动开发预览。

## 功能与边界

项目由本地服务与桌面界面组成，主要面向 Windows x64 开发和运行。

未配置云模型凭据时，应用保持“模型待配置”；这不会自动切换为真实大模型或离线 Mock。可以在设置页主动开启离线演示模式，或在本地环境文件中将 `LLM_DEMO_MODE=true`。离线演示根据检索到的原文生成摘录式输出，不等同于真实模型回答。

Python 项目代码按 MIT License 授权；第三方库、字体及附带工具保留各自许可证，见 [`LICENSES.md`](LICENSES.md)。源码发布不代表所有未来回答都正确。使用者应核对来源原文。模型服务商的 API 请求依其服务条款处理。

桌面应用把文献和会话数据保存在本机。开发模式默认数据目录为 `backend/data`；已安装 Windows 版本使用当前 Windows 用户的数据目录。Windows API 密钥保护绑定当前用户。项目终端与文件访问使用当前操作系统用户权限，不提供 OS 级进程沙箱。

## 本地运行（Windows PowerShell）

需要 Python 3.11 或更高版本、Node.js 22.12 或更高版本（或 Node.js 24）、npm。桌面原生功能以 Windows x64 为主要支持目标。

1. 创建并激活 Python 虚拟环境，然后安装后端依赖：

   ```powershell
   py -3 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -r backend/requirements.txt
   Copy-Item backend/.env.example backend/.env
   ```

2. 在第一个终端启动后端：

   ```powershell
   Set-Location backend
   python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
   ```

3. 在第二个终端安装前端依赖并启动 Vite：

   ```powershell
   Set-Location frontend
   npm ci
   npm run dev
   ```

4. 打开 Vite 输出的网址（通常是 `http://localhost:5173`）。前端会把 `/api` 请求代理至本机 `127.0.0.1:8765`。

需要真实模型时，在应用“模型设置”页配置接口；也可以编辑本地 `backend/.env`。请勿把真实密钥提交到 GitHub。只想体验合成样例时，在设置页选择“离线演示”，或将本地 `LLM_DEMO_MODE` 设为 `true`。

如果要使用桌面窗口开发模式，请查看 [`docs/windows-development.md`](docs/windows-development.md)。构建可分发安装器需要独立准备 Windows 原生工具链；在解决 `proxy-tools 0.1.0` 许可证标记冲突并完成发行核查前，本仓库不提供可分发安装器构建承诺。

## 测试与检查

在仓库根目录运行前端检查：

```powershell
Set-Location frontend
npm ci
npm run typecheck
npm run build
npm run test:markdown
node --experimental-strip-types --test tests/chatRunObserver.test.mjs
```

运行后端测试：

```powershell
python -m pip install -r backend/requirements.txt
python -m pytest backend/tests -q
```

仓库包含 GitHub Actions 基础检查工作流，分别运行后端定向测试与前端类型检查/构建。

## 目录

- `backend/app`：API、文献解析、检索、Agent 与本地存储。
- `backend/tests`：后端自动化测试。
- `frontend/src`：Vue 3 / TypeScript 界面；`frontend/public/licenses`：字体许可文本。
- `desktop`：Windows 桌面启动器、PyInstaller 与 Inno Setup 构建源码，以及 ripgrep 上游工具许可。
- `docs`：架构、隐私、开发和第三方许可说明。

## 许可证

本项目源代码按 [MIT License](LICENSE) 授权。第三方组件与字体继续遵循各自的许可证。具体说明及尚待澄清的第三方依赖事项见 [`LICENSES.md`](LICENSES.md)。

## English

ScholarFlow is a local research workspace for organizing papers, searching source passages, and answering literature questions with clickable citations. The primary interface is Simplified Chinese.

The repository contains source code, not a prebuilt installer or user data. It is primarily developed for Windows x64. The Python project source is licensed under MIT; third-party libraries, fonts, and bundled tools keep their own licenses. Cloud-model credentials are optional and must be configured explicitly. Offline demo mode is opt-in and does not represent a real LLM response.

See the sections above for setup and test commands. Review [`LICENSES.md`](LICENSES.md) before redistributing a desktop build.
