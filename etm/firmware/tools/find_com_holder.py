#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
find_com_holder.py —— 找出谁占用了指定串口（默认 COM33）

原理：枚举**全系统句柄**（NtQuerySystemInformation + SystemExtendedHandleInformation），
筛出名字里含 "\\Device" 且能解析到串口设备的句柄，再映射回 PID → 进程名。

需要管理员权限（当前会话是 Administrator，够用）。

用法：
  python find_com_holder.py                 # 默认找 COM33
  python find_com_holder.py COM5
  python find_com_holder.py --kill          # 找到后直接结束占用进程（谨慎）
"""
import argparse
import ctypes
import sys
from ctypes import wintypes

ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

SystemExtendedHandleInformation = 64
STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
DUPLICATE_SAME_ACCESS = 0x00000002
PROCESS_DUP_HANDLE = 0x0040
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class SYSTEM_HANDLE_TABLE_ENTRY_INFO_EX(ctypes.Structure):
    _fields_ = [
        ("Object", ctypes.c_void_p),
        ("UniqueProcessId", ctypes.c_void_p),
        ("HandleValue", ctypes.c_void_p),
        ("GrantedAccess", wintypes.ULONG),
        ("CreatorBackTraceIndex", wintypes.USHORT),
        ("ObjectTypeIndex", wintypes.USHORT),
        ("HandleAttributes", wintypes.ULONG),
        ("Reserved", wintypes.ULONG),
    ]


def query_handles():
    ntdll.NtQuerySystemInformation.restype = ctypes.c_long
    size = 1 << 20
    for _ in range(12):
        buf = ctypes.create_string_buffer(size)
        ret_len = wintypes.ULONG(0)
        status = ntdll.NtQuerySystemInformation(
            SystemExtendedHandleInformation, buf, size, ctypes.byref(ret_len))
        if status == STATUS_INFO_LENGTH_MISMATCH:
            size = max(size * 2, ret_len.value + (1 << 16))
            continue
        if status < 0:
            raise OSError(f"NtQuerySystemInformation 失败: 0x{status & 0xFFFFFFFF:08x}")
        count = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
        arr = ctypes.cast(ctypes.addressof(buf) + ctypes.sizeof(ctypes.c_void_p) * 2,
                          ctypes.POINTER(SYSTEM_HANDLE_TABLE_ENTRY_INFO_EX))
        return count, arr
    raise OSError("句柄表太大，放弃")


def process_name(pid):
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        n = wintypes.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            return buf.value
        return None
    finally:
        kernel32.CloseHandle(h)


def object_name_for(pid, handle):
    """在被查询进程里复制该句柄，再用 NtQueryObject 拿名字"""
    hproc = kernel32.OpenProcess(PROCESS_DUP_HANDLE, False, pid)
    if not hproc:
        return None
    try:
        dup = wintypes.HANDLE()
        ok = kernel32.DuplicateHandle(hproc, ctypes.c_void_p(handle),
                                      kernel32.GetCurrentProcess(), ctypes.byref(dup),
                                      0, False, DUPLICATE_SAME_ACCESS)
        if not ok:
            return None
        try:
            # ObjectNameInformation = 1
            ntdll.NtQueryObject.restype = ctypes.c_long
            for size in (512, 1024, 4096):
                buf = ctypes.create_string_buffer(size)
                ret = wintypes.ULONG(0)
                st = ntdll.NtQueryObject(dup, 1, buf, size, ctypes.byref(ret))
                if st >= 0:
                    # UNICODE_STRING { USHORT Len; USHORT MaxLen; PWSTR Buffer; }
                    length = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ushort))[0]
                    if length == 0:
                        return None
                    ptr = ctypes.cast(ctypes.addressof(buf) + 8, ctypes.POINTER(ctypes.c_void_p))[0]
                    return ctypes.wstring_at(ptr, length // 2)
        finally:
            kernel32.CloseHandle(dup)
        return None
    finally:
        kernel32.CloseHandle(hproc)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("port", nargs="?", default="COM33")
    ap.add_argument("--kill", action="store_true", help="结束占用进程")
    args = ap.parse_args()

    if not ctypes.windll.shell32.IsUserAnAdmin():
        print("⚠️ 需要管理员权限才能枚举全系统句柄", file=sys.stderr)

    count, arr = query_handles()
    print(f"系统句柄总数 = {count}，开始筛选（这一步可能要十几秒）...")

    hits = {}
    seen_pids = set()
    for i in range(count):
        e = arr[i]
        try:
            pid = int(e.UniqueProcessId or 0)
            obj = int(e.Object or 0)
            h = int(e.HandleValue or 0)
        except Exception:
            continue
        if pid <= 4 or obj == 0:
            continue
        if pid == ctypes.windll.kernel32.GetCurrentProcessId():
            continue
        # 只看名字像设备的句柄能省大量时间：先收集 PID，再逐个查
        hits.setdefault(pid, []).append(h)

    print(f"涉及 {len(hits)} 个进程，逐进程解析句柄名...")
    found = {}
    for pid, handles in hits.items():
        for h in handles:
            name = object_name_for(pid, h)
            if not name:
                continue
            if "Serial" in name or args.port in name or "VCP" in name or "usbser" in name:
                found.setdefault(pid, set()).add(name)

    if not found:
        print(f"\n没有找到明显持有 {args.port} 的进程（可能占用已释放，或句柄名不含串口特征）")
        return 1

    print(f"\n=== 疑似占用 {args.port} 的进程 ===")
    pids = []
    for pid, names in found.items():
        pname = process_name(pid) or "(无法取得路径)"
        print(f"  PID {pid:6d}  {pname}")
        for n in sorted(names):
            print(f"          handle -> {n}")
        pids.append((pid, pname))

    if args.kill:
        print("\n=== 结束这些进程 ===")
        for pid, pname in pids:
            try:
                h = kernel32.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
                if h:
                    ok = kernel32.TerminateProcess(h, 1)
                    kernel32.CloseHandle(h)
                    print(f"  {'已结束' if ok else '结束失败'} PID {pid} {pname}")
                else:
                    print(f"  打不开 PID {pid} {pname}（权限不足）")
            except Exception as ex:
                print(f"  PID {pid} 结束异常: {ex}")
    else:
        print("\n（加 --kill 可直接结束它们；或你手动关掉上面的程序）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
