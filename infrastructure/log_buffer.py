"""Buffer en memoria de los últimos logs, para mostrarlos en el panel.

Se conecta como un 'sink' de loguru. Mantiene los últimos N registros con un
id incremental para que el frontend pida solo los nuevos.
"""
from __future__ import annotations

import threading
from collections import deque

_MAXLEN = 600
_buffer: deque = deque(maxlen=_MAXLEN)
_lock = threading.Lock()
_seq = 0


def sink(message) -> None:
    """Sink de loguru: recibe el mensaje formateado con .record."""
    global _seq
    try:
        rec = message.record
        item = {
            "time": rec["time"].strftime("%H:%M:%S"),
            "level": rec["level"].name,
            "module": rec["name"].split(".")[-1],
            "message": rec["message"],
        }
    except Exception:  # noqa: BLE001
        item = {"time": "", "level": "INFO", "module": "", "message": str(message).strip()}
    with _lock:
        _seq += 1
        item["id"] = _seq
        _buffer.append(item)


def get_logs(after: int = 0, limit: int = 400) -> list[dict]:
    with _lock:
        items = [x for x in _buffer if x["id"] > after]
    return items[-limit:]
