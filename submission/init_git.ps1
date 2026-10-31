param(
  [string]$RemoteUrl = ''
)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not (Test-Path '.git')) { git init -b main }
git config user.name 'zhangfan'
git config user.email 'lrtzyf@gmail.com'
git add -A
git commit -m 'chore: prepare ScholarFlow competition submission'
if ($RemoteUrl) {
  if ((git remote) -contains 'origin') { git remote set-url origin $RemoteUrl }
  else { git remote add origin $RemoteUrl }
  git push -u origin main
}
git log --oneline -1
