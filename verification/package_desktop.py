"""Package the isolated Windows build and include verification metadata."""
import hashlib
import argparse
import json
import re
import shutil
import zipfile
from importlib.metadata import distribution
from datetime import datetime, timezone
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--backend-tests', type=int, required=True)
parser.add_argument('--label', default='')
args = parser.parse_args()
if args.label and (len(args.label) > 40 or any(not (char.isalnum() or char in '-_') for char in args.label)):
    parser.error('--label must contain only letters, digits, hyphens and underscores')
root = Path(__file__).resolve().parents[1]
bundle = root / "verification" / "desktop"
release_name = f"研序ScholarFlow-桌面验收版-{datetime.now().strftime('%Y%m%d')}"
if args.label:
    release_name += '-' + args.label
archive_path = root / "verification" / f"{release_name}.zip"
if archive_path.exists() and not args.label:
    release_name += "-文件编辑验收"
    archive_path = root / "verification" / f"{release_name}.zip"

for filename in ("LICENSE", "LICENSES.md", "README.md", "差距审计.md"):
    shutil.copy2(root / filename, bundle / filename)
acceptance_notes = {
    '模型Git闭环': '模型Git闭环验收-20261031.md',
    '工作树管理': '工作树验收-20261031.md',
    '工作树归档': '工作树归档验收-20261031.md',
    '终端隔离': '终端隔离验收-20261031.md',
    '会话目录切换': '会话目录切换验收-20261031.md',
    '同提交改动迁移': '同提交改动迁移验收-20261031.md',
}
if args.label in acceptance_notes:
    shutil.copy2(root / 'verification' / acceptance_notes[args.label], bundle / '本批验收说明.md')
    readme = bundle / 'README.md'
    readme.write_text(readme.read_text(encoding='utf-8').replace(
        '(verification/' + acceptance_notes[args.label] + ')', '(本批验收说明.md)'), encoding='utf-8')
if args.label == '工作树管理':
    evidence = bundle / '验收记录'
    evidence.mkdir(exist_ok=True)
    for filename in ('worktrees-backend-final-tests.log', 'worktrees-runtime-recheck.log',
                     'worktrees-unit-tests.log', 'worktrees-desktop-tests.log',
                     'worktrees-frontend-build.log', 'worktrees-exe-smoke.log',
                     'worktrees-desktop-smoke.log', '对标证据-20261030.md',
                     'worktree-create-desktop.png', 'worktree-create-small-desktop.png',
                     'worktree-list-desktop.png', 'worktree-list-small-desktop.png'):
        shutil.copy2(root / 'verification' / filename, evidence / filename)
if args.label == '工作树归档':
    evidence = bundle / '验收记录' / '工作树归档'
    evidence.mkdir(parents=True, exist_ok=True)
    for filename in ('worktree-archive-backend-tests.log', 'worktree-archive-unit-tests.log',
                     'worktree-archive-desktop-tests.log', 'worktree-archive-frontend-build.log',
                     'worktree-archive-exe-smoke.log', 'worktree-archive-desktop-smoke.log',
                     'worktree-archive-review-desktop.png', 'worktree-archive-review-small-desktop.png',
                     '对标证据-20261030.md'):
        shutil.copy2(root / 'verification' / filename, evidence / filename)
if args.label == '终端隔离':
    test_log = (root / 'verification/terminal-isolation-backend-tests.log').read_text(encoding='utf-8-sig')
    totals = re.findall(r'=+ (\d+) passed, [^\r\n]* =+', test_log)
    if not totals or int(totals[-1]) != args.backend_tests:
        raise RuntimeError('A matching completed full backend test run is required before packaging')
    evidence = bundle / '验收记录' / '终端隔离'
    evidence.mkdir(parents=True, exist_ok=True)
    for filename in ('terminal-isolation-backend-tests.log', 'terminal-isolation-unit-tests.log',
                     'terminal-isolation-stress-tests.log', 'terminal-isolation-desktop-tests.log',
                     'terminal-isolation-build.log', 'terminal-isolation-exe-smoke.log',
                     'terminal-isolation-desktop-smoke.log', 'handoff-backend-diagnostic.log',
                     'terminal-http-stress-diagnostic.log', 'terminal-thread-state.log',
                     'terminal-desktop.png', 'terminal-small-desktop.png', '对标证据-20261030.md'):
        shutil.copy2(root / 'verification' / filename, evidence / filename)

if args.label == '同提交改动迁移':
    test_log = (root / 'verification/workspace-transfer-backend-tests.log').read_text(encoding='utf-8-sig')
    totals = re.findall(r'(?:^|\n)(?:=+\s*)?(\d+) passed[^\r\n]*', test_log)
    if not totals or int(totals[-1]) != args.backend_tests:
        raise RuntimeError('A matching completed full backend test run is required before packaging')
    evidence = bundle / '验收记录' / '同提交改动迁移'
    evidence.mkdir(parents=True, exist_ok=True)
    for filename in ('workspace-transfer-backend-tests.log', 'workspace-transfer-backend-first-pass.log',
                     'workspace-transfer-unit-tests.log', 'workspace-transfer-desktop-tests.log',
                     'workspace-transfer-frontend-build.log', 'workspace-transfer-typecheck.log',
                     'workspace-transfer-build.log', 'workspace-transfer-exe-smoke.log',
                     'workspace-transfer-desktop-smoke.log', 'workspace-transfer-command-crash-recheck.log',
                     'workspace-transfer-regression-workspace-switch.log', 'workspace-transfer-regression-file-changes.log',
                     'workspace-transfer-regression-worktrees.log',
                     'workspace-transfer-review-desktop.png', 'workspace-transfer-review-small-desktop.png',
                     'workspace-transfer-conflict-desktop.png', 'workspace-transfer-return-small-desktop.png',
                     'workspace-transfer-recovery-small-desktop.png', 'workspace-transfer-failure.png',
                     '对标证据-20261030.md'):
        shutil.copy2(root / 'verification' / filename, evidence / filename)

if args.label == '会话目录切换':
    test_log = (root / 'verification/workspace-switch-backend-tests.log').read_text(encoding='utf-8-sig')
    totals = re.findall(r'(?:^|\n)(?:=+\s*)?(\d+) passed[^\r\n]*', test_log)
    if not totals or int(totals[-1]) != args.backend_tests:
        raise RuntimeError('A matching completed full backend test run is required before packaging')
    evidence = bundle / '验收记录' / '会话目录切换'
    evidence.mkdir(parents=True, exist_ok=True)
    for filename in ('workspace-switch-backend-tests.log', 'workspace-switch-unit-tests.log',
                     'workspace-switch-edit-followup-tests.log',
                     'workspace-switch-desktop-tests.log', 'workspace-switch-frontend-build.log',
                     'workspace-switch-typecheck.log', 'workspace-switch-build.log',
                     'workspace-switch-exe-smoke.log', 'workspace-switch-desktop-smoke.log',
                     'workspace-switch-review-desktop.png', 'workspace-switch-review-small-desktop.png',
                     'workspace-switch-history-small-desktop.png', '对标证据-20261030.md',
                     'workspace-switch-regression-desktop-state.log', 'workspace-switch-regression-projects.log',
                     'workspace-switch-regression-worktrees.log', 'workspace-switch-regression-file-changes.log',
                     'workspace-switch-regression-plan-mode.log'):
        shutil.copy2(root / 'verification' / filename, evidence / filename)

notices = bundle / "third-party-licenses"
notices.mkdir(exist_ok=True)
for package in ("markdown-it", "highlight.js", "katex", "@mdit/plugin-katex", "@mdit/plugin-tex", "@mdit/helper", "lucide-vue-next", "@xterm/xterm", "@xterm/addon-fit"):
    shutil.copy2(root / "frontend" / "node_modules" / package / "LICENSE", notices / (package.replace("/", "-").replace("@", "") + ".txt"))
shutil.copy2(root / 'desktop' / 'vendor' / 'ripgrep' / 'LICENSE-MIT', notices / 'ripgrep-MIT.txt')
pywinpty = distribution('pywinpty')
for license_path in pywinpty.files:
    if license_path.name == 'LICENSE.txt':
        shutil.copy2(pywinpty.locate_file(license_path), notices / 'pywinpty-MIT.txt')
        break
else:
    raise RuntimeError('pywinpty license is missing')

def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()

terminal_notices = root / 'desktop' / 'vendor' / 'terminal-licenses'
terminal_sources = json.loads((terminal_notices / 'sources.json').read_text(encoding='utf-8'))
for notice in terminal_sources:
    source = terminal_notices / notice['file']
    assert digest(source) == notice['sha256']
    shutil.copy2(source, notices / source.name)
shutil.copy2(terminal_notices / 'sources.json', notices / 'terminal-native-sources.json')

executable = bundle / "ScholarFlow" / "ScholarFlow.exe"
index = bundle / "ScholarFlow" / "_internal" / "frontend" / "index.html"
assert index.read_bytes() == (root / "frontend" / "dist" / "index.html").read_bytes()
rg_dir = executable.parent / '_internal' / 'vendor' / 'ripgrep'
rg_manifest = json.loads((rg_dir / 'manifest.json').read_text(encoding='utf-8'))
assert digest(rg_dir / 'rg.exe') == rg_manifest['executable_sha256']
manifest = {
    "built_at_utc": datetime.now(timezone.utc).isoformat(),
    "platform": "Windows desktop", "viewports": ["1440x900", "1100x700"],
    "executable_sha256": digest(executable), "frontend_index_sha256": digest(index),
    "backend_tests": args.backend_tests,
    "backend_test_run_note": (
        f"Full suite: {args.backend_tests} passed. Terminal-specific suite: 15 passed; repeated HTTP terminal lifecycle: 20 passed. Earlier full-run failure was a thread-exit assertion race; original terminal teardown hang is retained in diagnostic logs. Native PTY now runs in a separate worker per terminal."
        if args.label == '终端隔离' else (
        "Archive-specific suite: 26 passed; prior worktree suite: 25 passed. The latest full pytest run was started after these changes but produced no terminal result and remained at 81 percent, so it is not counted as a full pass. Packaged EXE and desktop archive smoke passed; all raw logs are retained in the evidence folder."
        if args.label == '工作树归档' else (
            "418 unique tests verified: latest full run had 417 passes and one subprocess-start failure while the concurrent frontend build temporarily removed dist/assets; after the build, all 15 runtime-ownership tests passed, including that case. Worktree-specific suite: 25 passed. Original failure and retry logs are retained in the evidence folder."
            if args.label == '工作树管理' else None))),
    "ripgrep": rg_manifest,
    "terminal_components": {
        "pywinpty": pywinpty.version,
        "xterm": json.loads((root / 'frontend' / 'node_modules' / '@xterm' / 'xterm' / 'package.json').read_text())['version'],
        "fit_addon": json.loads((root / 'frontend' / 'node_modules' / '@xterm' / 'addon-fit' / 'package.json').read_text())['version'],
        "native_libraries": {p.name: digest(p) for p in (executable.parent / '_internal' / 'winpty').iterdir() if p.suffix in ('.dll', '.exe', '.pyd')},
        "upstream_notices": terminal_sources,
    },
    "terminal_worker_verification": (
        "One hidden native PTY worker per session, assigned to a Windows job before startup configuration; bounded private pipes/queues, 10-second startup and 20-second heartbeat limits. Real crash/freeze/startup-timeout tests reap descendants and preserve HTTP service. Windowed EXE private entry, six lifecycle cycles, no secondary listener/runtime owner, crash recovery, and absence of _winpty from the primary process module map verified."
        if args.label == '终端隔离' else None),
    "project_search_verification": "bundled official ripgrep; recursive UTF-8 literal/regex/case/word search, directory and include/exclude globs, project ignore and hidden controls; verified Unicode snippets and file hashes, snapshot pagination, bounded incomplete results, scoped execute/plan model dispatch; desktop browser 1440x900/1100x700 with vertical/horizontal preview location, references, cancellation and stale response/file/page rejection, SQLite draft CAS and different-port restart; packaged EXE search/hash/version/license/restart checks",
    "answer_rendering_verification": "Highlight.js with 11 explicit grammars, literal unknown-language fallback, KaTeX with local fonts and untrusted commands disabled; individual code copying, TSV copying and UTF-8 CSV downloads; controlled desktop browser checks for clipboard success/fallback/failure/retry, input focus and selection, streaming updates, stale-copy guards, cleanup, keyboard and 1440x900/1100x700 layouts",
    "packaged_static_verification": f"EXE HTTP response for index and all {sum(p.is_file() for p in (root / 'frontend' / 'dist' / 'assets').rglob('*'))} dist assets matches the current build byte-for-byte, including local math fonts; packaged runtime/restart/Git/terminal smoke passed",
    "terminal_verification": "bundled pywinpty ConPTY and lazy xterm.js; real PowerShell/cmd Unicode file execution, idempotent lost-create recovery, atomic parser-reply acknowledgement and replay cutoff, WebSocket reconnect/reload without process restart, duplicate-controller refusal, bounded sessions/output/input, PTY resize, keyboard tabs/divider, Ctrl-C/stop/close; desktop Chromium 1440x900/1100x700 and packaged EXE Unicode/WebSocket/resize/descendant-crash checks",
    "git_verification": "status/diff/history and existing worktrees; reviewed manual and agent stage/unstage/commit, durable operation journal and SQLite commit drafts; agent-owned approvals cannot be applied via manual endpoints; persisted chat/task review links, deny/plan/hook/conflict checks, Unicode filenames, pending-permission invalidation on restart and completed-result recovery without replay; HEAD/branch/index/config/effective-attributes/content checks, literal NUL pathspecs, unborn and rename handling; standard hooks/signing, Windows suspended process bound to kill-on-close job; real Git, desktop browser 1440x900/1100x700 and EXE local HTTP model/restart checks",
    "worktree_verification": "real Git detached/new-branch creation from local commits, independent files/index and new-conversation project binding; durable request deduplication, draft CAS, lifecycle journal and interrupt reconciliation; reviewed clean-only managed checkout removal with private commit ref, active foreground/background/nested-project/terminal guards, dirty/ignored/index-flag/submodule/lock/identity/revision checks; original-path detached restore preserves project and conversation identity; desktop browser 1440x900/1100x700, lost responses/two windows/new-port restart and packaged EXE Git/filesystem/HTTP recovery verified",
    "subagent_policy_verification": "execution checks the allowed read-only tool names before dispatch, including injected writes, unlisted tools and custom MCP calls; deterministic model unit test",
    "worktree_archive_verification": "reviewed content/index/HEAD/branch/identity/config snapshots; temporary-index working snapshot and separate original-index commit, protected Git ref and fsynced byte-exact ZIP before checkout preparation; original branch retained, no forced worktree removal; ignored files preserved only in local ZIP with per-file deletion journal; original-path detached no-checkout restoration verifies ZIP SHA256 and writes original bytes; changed files/index on recovery are not overwritten; real Git unit, desktop browser lost-response/two-window/download/new-port and packaged EXE binary/staged/untracked/ignored/restart checks",
    "workspace_verification": "immutable project roots and scoped read tools; durable update/create/delete proposals and before/after hashes; Windows directory/file handles, stale-file conflict and crash reconciliation; agent approval/plan/hook tests; two desktop browser sizes, cross-port editor draft, diff/apply/undo/conflict/history and frontend/background approval; packaged EXE file update/undo/create passed",
    "archive_verification": "real SQLite/HTTP/SSE in two desktop browser contexts, revision conflicts and different-port restart; packaged EXE HTTP restart passed",
    "search_verification": "real SQLite/HTTP/SSE desktop search, excerpts, pagination, stale-query/page rejection, cache deletion, focus and bounded signals; packaged EXE search/pagination/restart passed",
    "planning_verification": "exact read-tool allowlist before hooks/approval/execution; desktop review and explicit execution, duplicate claims and retries, background planning, different-port mode/draft restore; packaged EXE claim/retry/restart passed",
    "runtime_ownership_verification": "OS file lock acquired before database migration/recovery; authenticated loopback activation with instance-ID checks; real process crash/live-thread exclusion and controlled uncertain-write skip passed; packaged duplicate EXE exits without new listener/task recovery, independent data runs concurrently, crash takeover passed",
    "compaction_verification": "persistent incremental summary covers all eligible older history with four recent originals; bounded input batches, source fingerprint invalidation, partial resume, cancellation, scoped original/evidence recall, and separate provider/estimated/unknown call accounting tested; two desktop browser contexts exercise conflicts, source focus, commands, retry and cold-origin restart; packaged EXE cache/restart/incremental update passed",
    "limitations": ["native window interaction and graceful close remain unverified",
                    "native folder picker is wired but not verified through actual native interaction",
                    "project read/edit tools are scoped at application level; external MCP and hooks are not constrained by this boundary",
                    "project editing supports UTF-8 files up to 256 KiB; writes are journaled but in-place and not crash-atomic; partial writes require manual repair",
                    "Git must be installed locally; no remote sync or special merge/rebase/submodule workflow; manual and agent writes are capped at 1000 paths and 256 KiB review, staging files at 2 MB each; external concurrent Git is not fully isolated and configured hooks may change content; stopped/restarted pending agent reviews require a new proposal; a running Git operation finishes or hits its process timeout rather than immediate per-task termination",
                    "plain worktree removal requires a clean app-created checkout; reviewed archive preserves working files and original staged version in Git snapshots and ignored files only in a local ZIP; archive limits are 10000 ordinary files/512 MiB/64 directory levels including ignored files; no symlinks/junctions/submodules/nested repositories/special merge or sparse-index states, ACL/timestamp/empty-directory preservation or complete Git backup; snapshots and ZIPs are retained without an archive-history/cleanup UI; no existing-conversation handoff, model worktree tools, external adoption or OS sandbox; restore needs the original repository and management records, uses detached HEAD and does not rebind old execution checkpoints",
                    "project content search is bounded to UTF-8 nonbinary files up to 2 MB, depth 64, 2000 matching lines and engine/verification time/output caps; standard Rust regex is single-line, links/internal dependency folders are excluded; no search-and-replace or filesystem watcher, scans rerun for pagination",
                    "manual terminal and model project commands use current-user local shell privileges, not an OS sandbox; initial cwd alone is project-scoped; model commands are approval-gated and journaled with bounded output, while manual terminals remain separate",
                    "terminal sessions and 512 Ki-character output rings are in memory; backend restart ends sessions, 10-minute disconnection ends processes, and user input with unknown delivery is never automatically replayed; no persistent screen snapshot, shell integration, WSL or PowerShell 7 selection",
                    "historical file snapshot UI is not implemented",
                    "answer rendering actions were verified with controlled clipboard fixtures in desktop Chromium, not actual Windows clipboard interaction; long code/result folding and configurable code themes are not implemented",
                    "project context is limited to 8 files, 8000 initial characters each, UTF-8 text under 2 MB; project rules total at most 32 KiB",
                    "summaries are lossy; offline/error extraction truncates details and real-model semantic retention is unverified",
                    "four recent raw turns and tool output do not yet have a model-specific overall context token budget",
                    "compaction accounting survives deletion; other model calls still lack a complete durable usage ledger",
                    "planning restricts application tool dispatch, not an OS-level sandbox; application bookkeeping still persists",
                    "offline planning is an explicitly labeled example, not a model-generated task-specific plan",
                    "same-data execution uses one backend per directory, not distributed task leases; network filesystem locks are unverified",
                    "activation acknowledgement was verified, but native focus/minimize/restore interactions were not",
                    "conversation search uses literal substring scans; dedicated indexing and full Unicode case folding remain incomplete",
                    "desktop-state restart was validated using isolated HTTP processes, not a native window click-through",
                    "real model streaming and real MCP write recovery were not exercised",
                    "foreground stream output after the most recent periodic checkpoint may be lost",
                    "a foreground retry can duplicate an external write if its previous outcome is unknown",
                    "an interrupted background operation needs explicit review before retry or skip",
                    "older tasks without checkpoints can only be restarted as a new run"],
}
if args.label == '会话目录切换':
    manifest['backend_test_run_note'] = f'Full suite: {args.backend_tests} passed. Workspace-switch suite: 18 passed. Desktop attachment/return and draft CAS verified in two viewports. Existing-conversation attachment changes do not transfer Git state.'
    manifest['workspace_switch_verification'] = 'Same-repository managed checkout attachment/return, durable review and atomic conversation/draft/journal update, idempotent retries across EXE restarts, binding-generation guards for A-B-A, preserved Git files/index and explicitly scoped editor draft, cleared relative references, two-window conflict resolution, fresh edit/undo proposals, Tab/Escape and 1440x900/1100x700 desktop browser verification; all 83 HTTP assets matched current dist.'
    manifest['limitations'].append('Reviewed conversation switches keep checkout contents/index/refs/terminals in place. Moving Git state, merging conflicts, shared-directory coordination and interrupted transfer recovery remain unimplemented; editor draft remains with its original project, relative file references must be selected again, and pre-switch executions stay invalid even after returning.')
if args.label == '同提交改动迁移':
    manifest['backend_test_run_note'] = f'Final full suite: {args.backend_tests} passed. Transfer-specific suite: 29 passed. Earlier full run and crash-test recheck are retained separately; see acceptance notes.'
    manifest['workspace_transfer_verification'] = 'Same-HEAD uncommitted Git state transfer and return; byte-exact binary, staged/unstaged/untracked separation; durable dual-side backup and mutation journal; actual process death, token-owned Git lock recovery, source-cleanup protection, later edits, rollback, retained staged blobs after Git GC, final semantic Git-state checks; overlapping-project activity coordination and invalidation of affected chat scopes; two desktop sizes, restart recovery UI, lost-response idempotency and packaged EXE HTTP/filesystem verification.'
    manifest['limitations'].append('Transfers currently require identical HEADs and a clean destination. Branches and commits stay in place; divergent history merging and automatic running-task interruption remain unsupported. Unknown or partially written contents require manual backup inspection. Ignored files stay local; ignore-rule changes with ignored files are refused. ZIP index restoration depends on the original repository and retained private refs. Native window interaction and Codex pixel parity remain unverified.')
(bundle / "build-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
files = sorted(p for p in bundle.rglob("*") if p.is_file())
assert not any(p.name == ".env" for p in files)
with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
    for path in files:
        archive.write(path, Path(release_name) / path.relative_to(bundle))
with zipfile.ZipFile(archive_path) as archive:
    assert archive.testzip() is None
print(json.dumps({"archive": str(archive_path), "bytes": archive_path.stat().st_size,
                  "files": len(files), "sha256": digest(archive_path)}, ensure_ascii=False))
