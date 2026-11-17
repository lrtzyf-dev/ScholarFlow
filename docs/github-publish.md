# 上传到 GitHub

这个压缩包已包含 README、授权协议（LICENSE）、忽略规则和 GitHub Actions 基础检查。将 ZIP 解压到一个新目录，然后把该目录作为仓库根目录推送。

## 用 Git 推送

1. 在 GitHub 创建一个空仓库。上传现有文件时不要勾选自动创建 README、许可证或 `.gitignore`，因为这些文件已经在压缩包中。
2. 在 PowerShell 进入解压后的 `ScholarFlow-GitHub-开源源码-build3` 目录，执行：

   ```powershell
   git init -b main
   git add .
   git status --short
   git commit -m "Initial open-source release"
   git remote add origin <你的 GitHub 仓库地址>
   git push -u origin main
   ```

3. 推送完成后，在仓库设置中选择合适的公开可见性。第一次公开前请再次检查提交列表和 GitHub Secret Scanning 结果。

也可以把 ZIP 作为 GitHub Release 的源码交付附件；那只会发布压缩包，不会自动把压缩包里的文件展开成代码仓库。
