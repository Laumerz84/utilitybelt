"""Every process's basics in ONE Windows call (NtQuerySystemInformation, SystemProcessInformation).

psutil reads these one process at a time, and for processes it may not open it falls back to a
whole-system query per process: 400 processes took 1.5-5 s per scan on a busy PC (2026-11-12),
and the dashboard scanned every 3 s, so the window stuttered. This takes a few milliseconds and needs
no access to the processes themselves.

    snapshot() -> [{"pid", "ppid", "name", "threads", "created", "cpu_time", "rss"}], or None off Windows
"""
from __future__ import annotations

import ctypes
import sys

SYSTEM_PROCESS_INFORMATION = 5
STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
EPOCH_DIFF = 11644473600          # seconds from 1601-01-01 (FILETIME) to 1970-01-01

# Offsets in SYSTEM_PROCESS_INFORMATION on 64-bit Windows.
NEXT, THREADS, CREATE, USER, KERNEL = 0, 4, 32, 40, 48
NAME_LEN, NAME_BUF = 56, 64
PID, PPID, WORKING_SET = 80, 88, 144

_buf = None
_size = 1 << 20


def snapshot() -> list[dict] | None:
    global _buf, _size
    if sys.platform != "win32" or ctypes.sizeof(ctypes.c_void_p) != 8:
        return None
    ntdll = ctypes.WinDLL("ntdll")
    query = ntdll.NtQuerySystemInformation
    query.restype = ctypes.c_ulong
    query.argtypes = [ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
    needed = ctypes.c_ulong(0)
    for _ in range(5):
        if _buf is None or len(_buf) < _size:
            _buf = ctypes.create_string_buffer(_size)
        status = query(SYSTEM_PROCESS_INFORMATION, _buf, _size, ctypes.byref(needed))
        if status == STATUS_INFO_LENGTH_MISMATCH:
            _size = max(_size * 2, needed.value + (1 << 16))
            continue
        if status != 0:
            return None
        break
    else:
        return None

    base = ctypes.addressof(_buf)
    u32 = lambda off: ctypes.c_uint32.from_address(base + off).value      # noqa: E731
    i64 = lambda off: ctypes.c_int64.from_address(base + off).value       # noqa: E731
    u64 = lambda off: ctypes.c_uint64.from_address(base + off).value      # noqa: E731
    out, off = [], 0
    while True:
        pid = u64(off + PID)
        name_len = ctypes.c_uint16.from_address(base + off + NAME_LEN).value
        name_ptr = u64(off + NAME_BUF)
        name = ctypes.wstring_at(name_ptr, name_len // 2) if name_ptr and name_len else (
            "System Idle Process" if pid == 0 else "?")
        created = i64(off + CREATE)
        out.append({
            "pid": pid,
            "ppid": u64(off + PPID),
            "name": name,
            "threads": u32(off + THREADS),
            "created": created / 1e7 - EPOCH_DIFF if created else 0.0,
            "cpu_time": (i64(off + USER) + i64(off + KERNEL)) / 1e7,
            "rss": u64(off + WORKING_SET),
        })
        step = u32(off + NEXT)
        if not step:
            break
        off += step
    return out


if __name__ == "__main__":
    import time
    t = time.perf_counter()
    rows = snapshot() or []
    print(f"{len(rows)} processes in {1000 * (time.perf_counter() - t):.1f} ms")
    for r in sorted(rows, key=lambda r: -r["cpu_time"])[:5]:
        print(r)
