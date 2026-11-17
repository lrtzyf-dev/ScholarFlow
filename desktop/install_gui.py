"""Independent offline GUI. Importing this module never creates a window."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import json
import os
from pathlib import Path
from queue import Empty, Queue
import sqlite3
import threading

if __package__:
    from .install_tool import run_offline_action, load_action_trust
    from .install_shortcuts import create_shortcut
else:
    from install_tool import run_offline_action, load_action_trust
    from install_shortcuts import create_shortcut


@dataclass(frozen=True)
class InstallRequest:
    candidate: Path | None
    target: Path
    data: Path
    trusted_keys: Path
    direction: str = 'upgrade'
    closed_confirmed: bool = False
    shortcut_directory: Path | None = None

    def action_options(self):
        return dict(candidate=self.candidate, target=self.target, data=self.data,
                    trusted_keys=self.trusted_keys, direction=self.direction)

    def identity(self):
        return tuple(str(Path(value).absolute()) if value is not None else None
            for value in (self.candidate, self.target, self.data, self.trusted_keys, self.shortcut_directory)) + (self.direction,)


@dataclass(frozen=True)
class ActionEvent:
    action: str
    ok: bool
    value: dict | None = None
    error: str = ''


def _shortcut(request, receipt):
    default_data = Path(os.environ.get('APPDATA') or Path.home()) / 'ScholarFlow'
    if request.data.resolve() != default_data.resolve():
        raise ValueError('普通启动快捷方式不绑定自定义数据目录，已跳过创建')
    trust = load_action_trust(request.trusted_keys, candidate=request.candidate,
        target=request.target, data=request.data)
    from app.data_backup import recovery_root
    target = request.target.resolve()
    forbidden = [request.data, recovery_root(request.data),
        target.parent / ('.' + target.name + '-updates')]
    if request.candidate is not None:
        forbidden.append(request.candidate)
    return create_shortcut(request.target, request.shortcut_directory, trust, receipt, forbidden_roots=forbidden)


class InstallController:
    """One worker; the UI must consume completions through poll on its thread."""
    def __init__(self, *, action_runner=None, shortcut_writer=None):
        self._runner = action_runner or run_offline_action
        self._shortcut_writer = shortcut_writer or _shortcut
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='offline-install')
        self._events = Queue()
        self._lock = threading.Lock()
        self._busy = False
        self._closed = False
        self._verified = None
        self._generation = 0

    @property
    def can_close(self):
        with self._lock:
            return not self._busy

    def invalidate(self):
        with self._lock:
            self._verified = None
            self._generation += 1

    def submit(self, action, request):
        with self._lock:
            if self._closed:
                raise ValueError('窗口已关闭')
            if self._busy:
                raise ValueError('操作正在运行，请等待结果')
            if action not in {'verify', 'install', 'recover'}:
                raise ValueError('未知离线操作')
            if action != 'recover' and request.candidate is None:
                raise ValueError('请明确选择候选目录')
            if request.direction not in {'upgrade', 'rollback'}:
                raise ValueError('请选择升级或兼容回退')
            if action in {'install', 'recover'} and not request.closed_confirmed:
                raise ValueError('请先关闭研序，并勾选关闭确认')
            expected_id = None
            if action == 'install':
                if self._verified is None or self._verified[0] != request.identity():
                    raise ValueError('表单或候选尚未校验，请先校验当前选择')
                expected_id = self._verified[1]
            self._busy = True
            if action != 'install':
                self._verified = None
            self._executor.submit(self._execute, action, request, expected_id, self._generation)

    def _execute(self, action, request, expected_id, generation):
        try:
            options = request.action_options()
            if action == 'install':
                options['expected_candidate_id'] = expected_id
            result = self._runner(action, **options)
            if not isinstance(result, dict):
                raise RuntimeError('invalid worker result')
            if action == 'verify' and (result.get('integrity') != 'verified' or result.get('signature') != 'trusted'
                    or not isinstance(result.get('candidate_id'), str) or not result['candidate_id']):
                raise RuntimeError('invalid verification result')
            if action == 'install' and result.get('state') != 'committed':
                raise RuntimeError('installation outcome unknown')
            if action == 'recover' and result.get('state') not in {'committed', 'rolled_back', 'nothing_pending'}:
                raise RuntimeError('recovery outcome unknown')
            if action == 'install' and request.shortcut_directory is not None:
                try:
                    result = dict(result, shortcut=self._shortcut_writer(request, result))
                except Exception:
                    # Installation is committed; a failed optional shortcut must
                    # never make the operator repeat an installation transaction.
                    result = dict(result, shortcut={'state': 'failed', 'message': '安装已提交；快捷方式未创建，请单独检查所选目录。'})
            self._events.put((ActionEvent(action, True, result), request.identity(), generation))
        except (ValueError, OSError, sqlite3.Error) as exc:
            self._events.put((ActionEvent(action, False, error=str(exc)), None, generation))
        except Exception:
            self._events.put((ActionEvent(action, False, error='操作结果未能确认，请保留现场并执行恢复；不会自动重试。'), None, generation))

    def poll(self):
        try:
            event, identity, generation = self._events.get_nowait()
        except Empty:
            return None
        with self._lock:
            self._busy = False
            self._verified = (identity, event.value['candidate_id']) if event.ok and event.action == 'verify' and generation == self._generation else None
        return event

    def close(self):
        with self._lock:
            if self._busy:
                raise ValueError('操作正在运行，请等待结果后关闭')
            self._closed = True
        self._executor.shutdown(wait=False)


def main(initial=None):
    # Tk is optional for all headless CLI commands and controller verification.
    import tkinter as tk
    from tkinter import filedialog, ttk
    initial = initial or {}
    try:
        root = tk.Tk()
    except tk.TclError:
        raise ValueError('图形界面不可用，请使用 verify/install/recover 命令行入口') from None
    root.title('研序 · 离线安装与恢复')
    root.geometry('850x720')
    root.minsize(700, 600)
    root.columnconfigure(0, weight=1)
    root.rowconfigure(1, weight=1)
    frame = ttk.Frame(root, padding=16)
    frame.grid(row=0, column=0, sticky='nsew')
    frame.columnconfigure(1, weight=1)
    ttk.Label(frame, text='离线签名候选：安装 / 更新 / 兼容回退 / 中断恢复', font=('', 13, 'bold')).grid(row=0, column=0, columnspan=3, sticky='w', pady=(0, 10))
    ttk.Label(frame, text='工具须位于安装目录之外。明确选择数据目录；信任库须来自候选、程序和数据之外。', wraplength=760).grid(row=1, column=0, columnspan=3, sticky='w', pady=(0, 10))
    variables, editable = {}, []
    labels = [('candidate', '签名候选目录'), ('target', '安装目标目录'), ('data', '数据目录（须已存在）'),
              ('trusted_keys', '外部信任库 JSON'), ('shortcut_directory', '快捷方式目录（可选）')]
    for row, (name, label) in enumerate(labels, 2):
        value = initial.get(name)
        variables[name] = tk.StringVar(value=str(value) if value is not None else '')
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky='w', padx=(0, 8), pady=5)
        entry = ttk.Entry(frame, textvariable=variables[name])
        entry.grid(row=row, column=1, sticky='ew', pady=5)
        def browse(field=name):
            value = filedialog.askopenfilename(parent=root, title='选择外部信任库', filetypes=[('JSON', '*.json'), ('所有文件', '*')]) if field == 'trusted_keys' else filedialog.askdirectory(parent=root, mustexist=field in {'candidate', 'data', 'shortcut_directory'})
            if value:
                variables[field].set(value)
        button = ttk.Button(frame, text='选择…', command=browse)
        button.grid(row=row, column=2, padx=(8, 0))
        editable += [entry, button]
    direction = tk.StringVar(value=initial.get('direction', 'upgrade'))
    controls = ttk.Frame(frame)
    controls.grid(row=7, column=0, columnspan=3, sticky='w', pady=8)
    for text, value in [('首次安装 / 升级', 'upgrade'), ('明确兼容回退', 'rollback')]:
        radio = ttk.Radiobutton(controls, text=text, variable=direction, value=value)
        radio.pack(side='left', padx=(0, 16))
        editable.append(radio)
    closed = tk.BooleanVar(value=False)
    check = ttk.Checkbutton(frame, text='我已关闭使用此安装及数据目录的研序程序', variable=closed)
    check.grid(row=8, column=0, columnspan=3, sticky='w', pady=8)
    editable.append(check)
    ttk.Label(frame, text='快捷方式默认不创建；只支持标准 %APPDATA%/ScholarFlow 数据目录，不绑定自定义数据路径。\n选择目录后，仅安装成功时创建 ScholarFlow.lnk，已有同名文件会保留。\n关闭确认是操作者声明；程序及数据锁仍由安装事务核验。不会下载、自动重试或自动删除备份。', wraplength=760).grid(row=9, column=0, columnspan=3, sticky='w', pady=8)
    output = tk.Text(root, height=13, wrap='word', padx=12, pady=10)
    output.grid(row=1, column=0, sticky='nsew', padx=16, pady=(0, 16))
    output.configure(state='disabled')
    controller = InstallController()
    def show(text):
        output.configure(state='normal')
        output.delete('1.0', 'end')
        output.insert('end', text)
        output.configure(state='disabled')
    def set_busy(busy):
        for widget in editable:
            widget.configure(state='disabled' if busy else 'normal')
    def submit(action):
        try:
            for field in ('target', 'data', 'trusted_keys'):
                if not variables[field].get().strip():
                    raise ValueError('请明确填写安装目标、数据目录和外部信任库')
            req = InstallRequest(candidate=Path(variables['candidate'].get()) if variables['candidate'].get().strip() else None,
                target=Path(variables['target'].get()), data=Path(variables['data'].get()),
                trusted_keys=Path(variables['trusted_keys'].get()), direction=direction.get(), closed_confirmed=closed.get(),
                shortcut_directory=Path(variables['shortcut_directory'].get()) if variables['shortcut_directory'].get().strip() else None)
            controller.submit(action, req)
            set_busy(True)
            show('正在执行。请等待明确结果，运行期间不能重复提交或关闭此窗口。')
        except ValueError as exc:
            show(str(exc))
    actions = ttk.Frame(frame)
    actions.grid(row=10, column=0, columnspan=3, sticky='w', pady=8)
    for label, action in [('1. 校验候选', 'verify'), ('2. 执行安装 / 更新', 'install'), ('恢复中断事务', 'recover')]:
        button = ttk.Button(actions, text=label, command=lambda action=action: submit(action))
        button.pack(side='left', padx=(0, 10))
        editable.append(button)
    for variable in (*variables.values(), direction):
        variable.trace_add('write', lambda *args: controller.invalidate())
    def tick():
        event = controller.poll()
        if event is not None:
            set_busy(False)
            if not event.ok:
                show('操作未成功确认：' + event.error + '\n请保留目录和日志，必要时先执行恢复。')
            else:
                states = {'committed': '安装事务已提交。', 'rolled_back': '已恢复到事务前的兼容程序。', 'nothing_pending': '没有待恢复事务。'}
                headline = '候选验签与完整性通过；请核对候选身份后执行。' if event.action == 'verify' else states[event.value['state']]
                if event.value.get('candidate_identity_changed'):
                    headline += '\n警告：实际提交候选与预览身份不同，请核对下方实际回执。'
                show(headline + '\n\n' + json.dumps(event.value, ensure_ascii=False, indent=2) + '\n\n该回执不代表最终发行、净机旅程或长期验收已通过。')
        root.after(100, tick)
    def close():
        if not controller.can_close:
            show('事务正在运行，请等待结果。关闭窗口不会取消事务。')
            return
        controller.close()
        root.destroy()
    root.protocol('WM_DELETE_WINDOW', close)
    show('请先选择明确路径并校验候选。此窗口不会自动执行安装，也不会启动候选程序供交互使用。')
    tick()
    root.mainloop()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
