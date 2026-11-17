# 本地字体与许可

界面字体通过 `fonts.css` 加载，无第三方字体服务运行时请求。构建器将 WOFF2 内嵌到单文件 HTML；模块源码使用相对路径。字体文件都以 SIL Open Font License 1.1 许可分发，版权与许可原文随源码保留。

## Inter

- CSS 名称：`SF Inter`（仅 CSS 别名，字体文件未修改）。
- 原始字体：Inter，可变字重 400–600。
- 官方字体介绍：https://rsms.me/inter/
- 官方分发 CSS：https://fonts.googleapis.com/css2?family=Inter:wght@400..600&display=swap
- 许可来源：https://raw.githubusercontent.com/google/fonts/main/ofl/inter/OFL.txt
- 本地许可：`assets/fonts/Inter-OFL.txt`。
- 7 个上游 WOFF2 字符子集保留 Latin、Latin Extended、Greek、Cyrillic 与 Vietnamese 支持，总计 218,512 字节。字体 CSS 保留对应 `unicode-range`，未将字体只裁剪为当前界面文案。

## 中文无衬线字体

- CSS 名称：`SF Noto Sans SC`。
- 内部字体名称：`ScholarFlow CJK Sans`，为本项目派生字体；重命名用于区分经过裁剪的副本与原版。
- 基础字体：Noto Sans SC，Google Fonts 官方仓库。
- 原始文件：https://github.com/google/fonts/blob/main/ofl/notosanssc/NotoSansSC%5Bwght%5D.ttf
- 许可来源：https://raw.githubusercontent.com/google/fonts/main/ofl/notosanssc/OFL.txt
- 本地许可：`assets/fonts/NotoSansSC-OFL.txt`。
- 使用 FontTools 保留字重 400–600 的变量轴，裁剪到以下 Unicode 范围，再以 Brotli 编码成 WOFF2：`U+0000–052F, U+2000–33FF, U+4E00–9FFF, U+F900–FAFF, U+FE00–FFFF`。
- 这些范围实际包含 20,976 个基本汉字、标点、全角字符、数学符号及原字体包含的其他相应字符；输入不限定为当前页面文案。上游原字体不包含基本汉字区最后 16 个字符（U+9FF0–9FFF），这些罕见字使用系统字体回退。原字体未包含的字符与范围外的扩展汉字由系统中文无衬线备选字体接续。
- 派生过程保留原字体版权和许可条目，并更名家族/完整/PostScript 名称为 ScholarFlow CJK Sans。

## 使用约定

推荐主界面字体链：`"SF Inter", "SF Noto Sans SC", "PingFang SC", "Microsoft YaHei UI", "Microsoft YaHei", "Noto Sans CJK SC", system-ui, sans-serif`。中文应用界面不使用衬线字体；论文版式示例可独立选择正文衬线字体。

`font-display: swap` 在字体解码前允许系统字体展示。所有字号、字重、行高仍由设计 token 控制，不在字体文件里固化。当前包已经包含编译后的 WOFF2，用户运行或标准库构建 HTML 不需要安装 FontTools 或 Brotli。

升级字体时应从官方来源下载，保留 OFL 原文，更新字符覆盖和大小记录，并检查新的字体 CSS 与资产一并打包。

## 本次资产校验

- 中文 WOFF2：5,637,892 字节，23,806 个字形。
- 全部 WOFF2：5,856,404 字节。
- 中文 WOFF2 SHA-256：`b6a61934d4ca55c5a328c87d36d6059be64e4fcb9927ae0e2a7579db8982f1e8`。
- 基础 TTF SHA-256：`a3041811a78c361b1de50f953c805e0244951c21c5bd412f7232ef0d899af0da`。
