"""Lightweight host/process metrics for the admin panel.

The Raspberry Pi path reads Linux procfs/sysfs files directly. That keeps the
monitor dependency-free and cheap enough to poll from the UI every few seconds.
"""
from __future__ import annotations

import os
import platform
import resource
import threading
import time
from pathlib import Path


class SystemMonitor:
    def __init__(self):
        self._lock = threading.Lock()
        self._last_cpu: tuple[int, int] | None = None
        self._last_process_ticks: int | None = None

    def snapshot(self) -> dict:
        with self._lock:
            cpu_total, cpu_idle = self._read_cpu_stat()
            process_ticks = self._read_process_ticks()
            cpu_percent = self._cpu_percent(cpu_total, cpu_idle)
            process_cpu_percent = self._process_cpu_percent(cpu_total, process_ticks)
            if cpu_total is not None and cpu_idle is not None:
                self._last_cpu = (cpu_total, cpu_idle)
            if process_ticks is not None:
                self._last_process_ticks = process_ticks

        memory = self._read_memory()
        load = self._read_loadavg()
        return {
            "timestamp": time.time(),
            "host": platform.node() or "",
            "platform": platform.platform(),
            "cpu": {
                "percent": cpu_percent,
                "cores": os.cpu_count() or 1,
                "load_1m": load[0],
                "load_5m": load[1],
                "load_15m": load[2],
            },
            "memory": memory,
            "process": {
                "cpu_percent": process_cpu_percent,
                "rss_mb": self._process_rss_mb(),
                "threads": threading.active_count(),
                "fds": self._fd_count(),
            },
            "temperature_c": self._temperature_c(),
            "uptime_seconds": self._uptime_seconds(),
        }

    def _cpu_percent(self, cpu_total: int | None, cpu_idle: int | None) -> float | None:
        if cpu_total is None or cpu_idle is None or self._last_cpu is None:
            return None
        last_total, last_idle = self._last_cpu
        total_delta = cpu_total - last_total
        idle_delta = cpu_idle - last_idle
        if total_delta <= 0:
            return None
        value = (1 - idle_delta / total_delta) * 100
        return round(max(0.0, min(100.0, value)), 1)

    def _process_cpu_percent(self, cpu_total: int | None, process_ticks: int | None) -> float | None:
        if (
            cpu_total is None
            or process_ticks is None
            or self._last_cpu is None
            or self._last_process_ticks is None
        ):
            return None
        last_total, _last_idle = self._last_cpu
        total_delta = cpu_total - last_total
        process_delta = process_ticks - self._last_process_ticks
        if total_delta <= 0 or process_delta < 0:
            return None
        cores = os.cpu_count() or 1
        return round(max(0.0, process_delta / total_delta * cores * 100), 1)

    @staticmethod
    def _read_cpu_stat() -> tuple[int | None, int | None]:
        try:
            line = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0]
            parts = [int(v) for v in line.split()[1:]]
        except (OSError, IndexError, ValueError):
            return None, None
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
        return sum(parts), idle

    @staticmethod
    def _read_process_ticks() -> int | None:
        try:
            raw = Path("/proc/self/stat").read_text(encoding="utf-8")
            after_name = raw.rsplit(")", 1)[1].strip().split()
            utime = int(after_name[11])
            stime = int(after_name[12])
        except (OSError, IndexError, ValueError):
            return None
        return utime + stime

    @staticmethod
    def _read_memory() -> dict:
        values: dict[str, int] = {}
        try:
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                name, raw = line.split(":", 1)
                values[name] = int(raw.strip().split()[0]) * 1024
        except (OSError, IndexError, ValueError):
            total = SystemMonitor._total_memory_bytes_fallback()
            return {
                "total_mb": _bytes_to_mb(total),
                "available_mb": None,
                "used_mb": None,
                "used_percent": None,
            }

        total = values.get("MemTotal")
        available = values.get("MemAvailable")
        if available is None:
            available = values.get("MemFree", 0) + values.get("Buffers", 0) + values.get("Cached", 0)
        used = total - available if total is not None and available is not None else None
        used_percent = round(used / total * 100, 1) if total and used is not None else None
        return {
            "total_mb": _bytes_to_mb(total),
            "available_mb": _bytes_to_mb(available),
            "used_mb": _bytes_to_mb(used),
            "used_percent": used_percent,
        }

    @staticmethod
    def _total_memory_bytes_fallback() -> int | None:
        try:
            return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        except (AttributeError, OSError, ValueError):
            return None

    @staticmethod
    def _process_rss_mb() -> float | None:
        try:
            for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)
        except (OSError, IndexError, ValueError):
            pass
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if rss <= 0:
            return None
        # Linux reports KiB; macOS reports bytes.
        divisor = 1024 if platform.system() != "Darwin" else 1024 * 1024
        return round(rss / divisor, 1)

    @staticmethod
    def _fd_count() -> int | None:
        try:
            return len(list(Path("/proc/self/fd").iterdir()))
        except OSError:
            return None

    @staticmethod
    def _temperature_c() -> float | None:
        try:
            raw = Path("/sys/class/thermal/thermal_zone0/temp").read_text(encoding="utf-8").strip()
            return round(int(raw) / 1000, 1)
        except (OSError, ValueError):
            return None

    @staticmethod
    def _uptime_seconds() -> float | None:
        try:
            return round(float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0]), 1)
        except (OSError, IndexError, ValueError):
            return None

    @staticmethod
    def _read_loadavg() -> tuple[float | None, float | None, float | None]:
        try:
            one, five, fifteen = os.getloadavg()
            return round(one, 2), round(five, 2), round(fifteen, 2)
        except OSError:
            return None, None, None


def _bytes_to_mb(value: int | None) -> float | None:
    return round(value / 1024 / 1024, 1) if value is not None else None
