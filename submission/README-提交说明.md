# 研序 ScholarFlow 竞赛提交说明

本目录用于整理“传智杯 AI 大模型创新应用挑战赛”科研助手 Agent 赛道的提交材料。当前版本面向 **Windows 桌面端**，验收尺寸为 1440×900 和 1100×700。

## 已具备的提交材料

| 材料 | 文件或位置 | 状态 |
| --- | --- | --- |
| 源码 | 仓库根目录 `backend/`、`frontend/`、`desktop/` | 已整理 |
| 项目 README | 根目录 `README.md` | 已补齐运行、架构、团队与安全说明 |
| 环境模板 | 根目录 `.env.example`、`backend/.env.example` | 已提供 |
| 依赖清单 | `requirements.txt`、`backend/requirements.txt`、`frontend/package.json` | 已提供 |
| 技术文档 | `研序ScholarFlow-技术文档.pdf` | 已生成，页数不超过 30 页 |
| 技术文档源稿 | `技术文档.md` | 与 PDF 同步保存 |
| 演示视频脚本 | `演示视频脚本.md` | 约 7 分钟普通话脚本 |
| 验收与校验 | `材料清单.md`、`check_submission.py` | 已提供 |
| Windows EXE | `verification/desktop/ScholarFlow/ScholarFlow.exe` | 本地验收产物，建议作为 Release 附件 |

## 托管平台状态

当前工作目录此前没有 Git 仓库，也没有可用的 GitHub/Gitee 远程地址或登录授权。本轮会初始化本地 Git、提交源码和材料，并生成可上传的源代码包；待提供远程仓库 URL 和推送凭据后，再执行 `git remote add`、推送和可见性确认。文档不伪造仓库地址或公网演示地址。

## 提交前动作

1. 在 GitHub 或 Gitee 创建空仓库，设置为 Public 或给评审账号授权。
2. 将本地仓库的 `main` 分支推送到远程。
3. 上传 `研序ScholarFlow-技术文档.pdf` 和 Windows EXE 作为 Release/附件。
4. 录制 `演示视频脚本.md` 对应的 5–8 分钟、1080p 以上 MP4，并在大赛官网填写视频地址或上传视频。
5. 将在线演示地址、评审账号和有效期补入 `在线演示说明.md`。当前版本没有公网部署地址，不能以本地地址代替。

## 安全检查

- 真实 API Key 不进入源码、Git 历史、README、PDF 或 ZIP。
- 评审可直接使用 Mock 模式运行；真实模型通过环境变量或应用“模型与设置”页配置。
- `reference/`、运行时数据库、虚拟环境、前端依赖目录和临时验收数据不进入源码提交。
