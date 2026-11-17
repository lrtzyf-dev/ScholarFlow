"""Replace a local unsigned PyInstaller EXE icon with full backup and rollback.

python desktop/update_exe_icon.py --apply
python desktop/update_exe_icon.py --restore <backup-directory>

The default target is the bundled ScholarFlow.exe. This never launches it. All
PE resources are edited in a copy; the original appended PyInstaller archive is
reattached byte-for-byte. The target must have no running ScholarFlow process.
"""
from __future__ import annotations
import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess

ROOT = Path(__file__).resolve().parent.parent
EXE = ROOT / 'ScholarFlow程序/ScholarFlow/ScholarFlow.exe'
ICON = ROOT / 'desktop/assets/app-icon.ico'
BUNDLE_ICON = EXE.parent / '_internal/assets/icon.ico'
BACKUPS = ROOT / 'ScholarFlow程序/图标备份'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def stopped():
    # Query only names/PIDs; never command lines or user data.
    result = subprocess.run(
        ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
         "@(Get-Process -Name ScholarFlow -ErrorAction SilentlyContinue).Count"],
        capture_output=True, text=True, check=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.stdout.strip() != '0':
        raise RuntimeError('Close ScholarFlow before changing its icon')


def layout(data):
    if data[:2] != b'MZ':
        raise ValueError('Not a PE executable')
    pe = struct.unpack_from('<I', data, 0x3c)[0]
    if data[pe:pe+4] != b'PE\0\0':
        raise ValueError('Invalid PE signature')
    count = struct.unpack_from('<H', data, pe+6)[0]
    optsize = struct.unpack_from('<H', data, pe+20)[0]
    opt = pe+24
    magic = struct.unpack_from('<H', data, opt)[0]
    if magic not in (0x10b, 0x20b):
        raise ValueError('Unsupported PE format')
    directory = opt + (112 if magic == 0x20b else 96)
    cert = struct.unpack_from('<II', data, directory+8*4)
    if cert != (0, 0):
        raise ValueError('Signed executables must be rebuilt and re-signed, not patched')
    sect = opt+optsize
    end = max(sum(struct.unpack_from('<II', data, sect+i*40+16)) for i in range(count))
    if not 0 < end <= len(data):
        raise ValueError('Invalid PE section bounds')
    return end, opt+64


def ico_entries(raw):
    reserved, kind, count = struct.unpack_from('<HHH', raw)
    if reserved or kind != 1 or not 1 <= count <= 64:
        raise ValueError('Invalid ICO header')
    entries = []
    for i in range(count):
        fields = struct.unpack_from('<BBBBHHII', raw, 6+16*i)
        width, height, colors, reserved, planes, bits, size, offset = fields
        if offset+size > len(raw):
            raise ValueError('Invalid ICO frame bounds')
        entries.append((fields[:6], raw[offset:offset+size]))
    return entries


def kernel():
    lib = ctypes.WinDLL('kernel32', use_last_error=True)
    lib.LoadLibraryExW.argtypes = [wintypes.LPCWSTR, wintypes.HANDLE, wintypes.DWORD]
    lib.LoadLibraryExW.restype = wintypes.HMODULE
    lib.FreeLibrary.argtypes = [wintypes.HMODULE]
    lib.FreeLibrary.restype = wintypes.BOOL
    lib.BeginUpdateResourceW.argtypes = [wintypes.LPCWSTR, wintypes.BOOL]
    lib.BeginUpdateResourceW.restype = wintypes.HANDLE
    lib.UpdateResourceW.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, wintypes.WORD, ctypes.c_void_p, wintypes.DWORD]
    lib.UpdateResourceW.restype = wintypes.BOOL
    lib.EndUpdateResourceW.argtypes = [wintypes.HANDLE, wintypes.BOOL]
    lib.EndUpdateResourceW.restype = wintypes.BOOL
    lib.FindResourceExW.argtypes = [wintypes.HMODULE, ctypes.c_void_p, ctypes.c_void_p, wintypes.WORD]
    lib.FindResourceExW.restype = wintypes.HANDLE
    lib.SizeofResource.argtypes = [wintypes.HMODULE, wintypes.HANDLE]
    lib.SizeofResource.restype = wintypes.DWORD
    lib.LoadResource.argtypes = [wintypes.HMODULE, wintypes.HANDLE]
    lib.LoadResource.restype = wintypes.HANDLE
    lib.LockResource.argtypes = [wintypes.HANDLE]
    lib.LockResource.restype = ctypes.c_void_p
    return lib


def identifier(value):
    return ctypes.c_void_p(value) if isinstance(value, int) else ctypes.cast(ctypes.c_wchar_p(value), ctypes.c_void_p)


def resource_names(lib, module, kind):
    names = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMODULE, ctypes.c_void_p, ctypes.c_void_p, wintypes.LPARAM)
    @callback_type
    def callback(_, __, name, ___):
        names.append(name if name <= 65535 else ctypes.wstring_at(name))
        return True
    lib.EnumResourceNamesW.argtypes = [wintypes.HMODULE, ctypes.c_void_p, callback_type, wintypes.LPARAM]
    lib.EnumResourceNamesW.restype = wintypes.BOOL
    if not lib.EnumResourceNamesW(module, identifier(kind), callback, 0):
        if ctypes.get_last_error() not in (1813, 1814):
            raise ctypes.WinError(ctypes.get_last_error())
    return names


def resource_languages(lib, module, kind, name):
    languages = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMODULE, ctypes.c_void_p, ctypes.c_void_p, wintypes.WORD, wintypes.LPARAM)
    @callback_type
    def callback(_, __, ___, language, ____):
        languages.append(language)
        return True
    lib.EnumResourceLanguagesW.argtypes = [wintypes.HMODULE, ctypes.c_void_p, ctypes.c_void_p, callback_type, wintypes.LPARAM]
    lib.EnumResourceLanguagesW.restype = wintypes.BOOL
    if not lib.EnumResourceLanguagesW(module, identifier(kind), identifier(name), callback, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    return languages


def read_resource(lib, module, kind, name, language):
    found = lib.FindResourceExW(module, identifier(kind), identifier(name), language)
    if not found:
        raise ctypes.WinError(ctypes.get_last_error())
    size = lib.SizeofResource(module, found)
    handle = lib.LoadResource(module, found)
    address = lib.LockResource(handle)
    if not address:
        raise ctypes.WinError(ctypes.get_last_error())
    return ctypes.string_at(address, size)


def patch_copy(target, original, icon):
    end, _ = layout(original)
    overlay = original[end:]
    if not overlay or original.rfind(b'MEI\014\013\012\013\016') < end:
        raise ValueError('Expected PyInstaller appended archive was not found')
    entries = ico_entries(icon)
    lib = kernel()
    module = lib.LoadLibraryExW(str(target), None, 2)  # Data file only; no code runs.
    if not module:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        groups = resource_names(lib, module, 14)
        group_languages = [(name, lang) for name in groups for lang in resource_languages(lib, module, 14, name)] or [(1, 0)]
        icon_ids = [name for name in resource_names(lib, module, 3) if isinstance(name, int)]
        first_id = max(icon_ids, default=0)+1
    finally:
        lib.FreeLibrary(module)
    if first_id+len(entries) > 65535:
        raise ValueError('Insufficient icon resource IDs')
    group = struct.pack('<HHH', 0, 1, len(entries))
    for index, (fields, data) in enumerate(entries):
        group += struct.pack('<BBBBHHIH', *fields, len(data), first_id+index)
    updater = lib.BeginUpdateResourceW(str(target), False)
    if not updater:
        raise ctypes.WinError(ctypes.get_last_error())
    committed = False
    try:
        def put(kind, name, language, data):
            buffer = ctypes.create_string_buffer(data)
            if not lib.UpdateResourceW(updater, identifier(kind), identifier(name), language, buffer, len(data)):
                raise ctypes.WinError(ctypes.get_last_error())
        for language in sorted({lang for _, lang in group_languages}):
            for index, (_, data) in enumerate(entries):
                put(3, first_id+index, language, data)
        for name, language in group_languages:
            put(14, name, language, group)
        if not lib.EndUpdateResourceW(updater, False):
            raise ctypes.WinError(ctypes.get_last_error())
        committed = True
    finally:
        if not committed:
            lib.EndUpdateResourceW(updater, True)
    updated = target.read_bytes()
    new_end, checksum_offset = layout(updated)
    # Rebuild the overlay unconditionally: UpdateResource may drop/move it.
    restored = bytearray(updated[:new_end] + overlay)
    struct.pack_into('<I', restored, checksum_offset, 0)
    target.write_bytes(restored)
    verify = target.read_bytes()
    if verify[new_end:] != overlay:
        raise ValueError('PyInstaller archive preservation failed')
    module = lib.LoadLibraryExW(str(target), None, 2)
    if not module:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        for name, language in group_languages:
            if read_resource(lib, module, 14, name, language) != group:
                raise ValueError('Icon group verification failed')
            for index, (_, data) in enumerate(entries):
                if read_resource(lib, module, 3, first_id+index, language) != data:
                    raise ValueError('Icon frame verification failed')
    finally:
        lib.FreeLibrary(module)
    return {'overlay_sha256': sha(overlay), 'overlay_bytes': len(overlay),
            'frames': len(entries), 'groups': len(group_languages),
            'original_pe_end': end, 'updated_pe_end': new_end}


def apply():
    stopped()
    original = EXE.read_bytes()
    icon = ICON.read_bytes()
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup = BACKUPS / stamp
    backup.mkdir(parents=True)
    shutil.copy2(EXE, backup/'ScholarFlow.exe')
    if BUNDLE_ICON.is_file():
        shutil.copy2(BUNDLE_ICON, backup/'window-icon.ico')
    staged = backup/'ScholarFlow-updated.exe'
    shutil.copy2(EXE, staged)
    details = patch_copy(staged, original, icon)
    record = {'executable': str(EXE), 'created_utc': stamp, 'original_sha256': sha(original),
              'updated_sha256': sha(staged.read_bytes()), 'icon_sha256': sha(icon), **details}
    (backup/'manifest.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    stopped()
    if sha(EXE.read_bytes()) != record['original_sha256']:
        raise RuntimeError('Executable changed during preparation; original left untouched')
    os.replace(staged, EXE)
    try:
        BUNDLE_ICON.write_bytes(icon)
        if sha(EXE.read_bytes()) != record['updated_sha256']:
            raise ValueError('Installed executable checksum differs')
    except BaseException:
        shutil.copy2(backup/'ScholarFlow.exe', EXE)
        if (backup/'window-icon.ico').exists():
            shutil.copy2(backup/'window-icon.ico', BUNDLE_ICON)
        raise
    print(json.dumps({'updated': str(EXE), 'backup': str(backup), 'frames': details['frames'],
                      'archive_preserved': True}, ensure_ascii=False))


def restore(backup):
    stopped()
    backup = backup.resolve()
    if not backup.is_relative_to(BACKUPS.resolve()):
        raise ValueError('Backup must be within the icon backup directory')
    record = json.loads((backup/'manifest.json').read_text(encoding='utf-8'))
    saved = (backup/'ScholarFlow.exe').read_bytes()
    if sha(saved) != record['original_sha256']:
        raise ValueError('Backup checksum mismatch')
    if sha(EXE.read_bytes()) != record['updated_sha256']:
        raise ValueError('Executable changed since this update; refusing to overwrite')
    staged = EXE.with_name('ScholarFlow-icon-restore.tmp')
    staged.write_bytes(saved)
    os.replace(staged, EXE)
    if (backup/'window-icon.ico').exists():
        shutil.copy2(backup/'window-icon.ico', BUNDLE_ICON)
    print(json.dumps({'restored': str(EXE)}, ensure_ascii=False))


if __name__ == '__main__':
    if os.name != 'nt':
        raise SystemExit('Windows is required')
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--apply', action='store_true')
    mode.add_argument('--restore', type=Path)
    args = parser.parse_args()
    apply() if args.apply else restore(args.restore)
