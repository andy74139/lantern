"""本機狀態（macOS）：記憶體、CPU、GPU。"""
import ctypes
import re
import subprocess
import threading
import time


def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return ""


def memory():
    total = int(_run(["sysctl", "-n", "hw.memsize"]).strip() or 0)
    vm = _run(["vm_stat"])
    m = re.search(r"page size of (\d+) bytes", vm)
    page = int(m.group(1)) if m else 16384
    pages = {k.strip().strip('"'): int(v) for k, v in re.findall(r'^(.+?):\s+(\d+)\.', vm, re.M)}
    anon = pages.get("Anonymous pages", pages.get("Pages active", 0))
    used = (anon - pages.get("Pages purgeable", 0)
            + pages.get("Pages wired down", 0)
            + pages.get("Pages occupied by compressor", 0)) * page
    used = max(0, min(used, total))
    level = _run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"]).strip()
    swap = _run(["sysctl", "-n", "vm.swapusage"])
    sm = re.search(r"used = ([\d.]+)M", swap)
    return {
        "total": total,
        "used": used,
        "pressure": {"1": "normal", "2": "warn", "4": "critical"}.get(level, "unknown"),
        "swap_used": int(float(sm.group(1)) * 1024 * 1024) if sm else 0,
    }


# ---------- CPU：讀核心累計的 tick，兩次讀數相減就是這段時間的使用率 ----------
_libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
_libc.mach_host_self.restype = ctypes.c_uint
_HOST_CPU_LOAD_INFO = 3
_cpu_lock = threading.Lock()
_cpu_prev = None            # (時間, (user, system, idle, nice))
_cpu_last = None            # 上一次算出來的百分比


def _cpu_ticks():
    info = (ctypes.c_uint * 4)()
    count = ctypes.c_uint(4)
    if _libc.host_statistics(_libc.mach_host_self(), _HOST_CPU_LOAD_INFO, ctypes.byref(info), ctypes.byref(count)) != 0:
        return None
    return tuple(info)


def cpu():
    global _cpu_prev, _cpu_last
    with _cpu_lock:
        now, ticks = time.time(), _cpu_ticks()
        if ticks is None:
            return None
        if _cpu_prev is None:
            _cpu_prev = (now, ticks)
            time.sleep(0.25)
            now, ticks = time.time(), _cpu_ticks()
        elif now - _cpu_prev[0] < 1.0 and _cpu_last is not None:
            return _cpu_last                      # 讀得太密就沿用上一次的結果
        d = [(b - a) & 0xFFFFFFFF for a, b in zip(_cpu_prev[1], ticks)]
        total = sum(d)
        if total:
            _cpu_last = round(100 * (total - d[2]) / total, 1)
        _cpu_prev = (now, ticks)
        return _cpu_last


def gpu():
    """Apple Silicon 的 GPU 使用率，來自繪圖驅動程式回報的統計（不需要 sudo）。"""
    out = _run(["ioreg", "-r", "-d", "1", "-w", "0", "-c", "IOAccelerator"])
    m = re.search(r'"Device Utilization %"=(\d+)', out)
    if not m:
        return None
    mem = re.search(r'"In use system memory"=(\d+)', out)
    name = re.search(r'"model" = "([^"]+)"', out)
    return {"percent": int(m.group(1)), "memory": int(mem.group(1)) if mem else None,
            "name": name.group(1) if name else None}


_ncpu = None


def status():
    global _ncpu
    if _ncpu is None:
        _ncpu = int(_run(["sysctl", "-n", "hw.ncpu"]).strip() or 0)
    return {**memory(), "cpu": cpu(), "ncpu": _ncpu, "gpu": gpu()}
