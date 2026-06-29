"""Punto de entrada: arranca el panel de administración (FastAPI).

El panel controla el bot (encender/apagar), gestiona los activos y muestra
estadísticas. El bot corre en un hilo en segundo plano del propio servidor.

  python main.py            -> http://127.0.0.1:8000
                           -> http://<ip-local>:8000 desde la misma WiFi
"""
from __future__ import annotations

import socket
import sys

import uvicorn
from loguru import logger
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from config.settings import DATA_DIR, LOGS_DIR, settings

console = Console()

HOST = "0.0.0.0"
PORT = 8000


def local_ip() -> str | None:
    """IP LAN principal para abrir el panel desde otro dispositivo."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return None


def setup_logging() -> None:
    from infrastructure.log_buffer import sink as log_sink

    logger.remove()
    logger.add(sys.stderr, level="INFO",
               format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}")
    logger.add(LOGS_DIR / "bot.log", level="DEBUG", rotation="10 MB", retention="14 days",
               encoding="utf-8")
    # Buffer en memoria para la pestaña de logs del panel.
    logger.add(log_sink, level="INFO", format="{message}")


def banner() -> None:
    lan_ip = local_ip()
    table = Table.grid(padding=(0, 2))
    table.add_column(justify="right", style="bold cyan")
    table.add_column()
    table.add_row("Panel local", f"[bold green]http://127.0.0.1:{PORT}[/]")
    if lan_ip:
        table.add_row("Panel celular", f"[bold green]http://{lan_ip}:{PORT}[/]")
    table.add_row("Modo de cuenta", f"[bold green]{settings.balance_mode}[/]")
    table.add_row("ALLOW_REAL", f"[bold red]{settings.allow_real}[/]")
    table.add_row("Activos (.env)", ", ".join(settings.assets))
    table.add_row("Permitir OTC", str(settings.allow_otc))
    table.add_row("Monto / Exp.", f"{settings.base_amount} / {settings.expiration_minutes} min")
    table.add_row("Payout mínimo", f"{settings.min_payout:.0%}")
    table.add_row("Persistencia", f"SQLite -> {DATA_DIR / 'bot.db'}")
    console.print(Panel(table, title="🤖 IQ Binary Bot — Panel (SOLO DEMO / ESTUDIO)",
                        border_style="cyan"))


def main() -> None:
    setup_logging()
    banner()

    if settings.iq_email == "tu_correo":
        logger.error("Configura IQ_EMAIL e IQ_PASSWORD en el archivo .env antes de correr.")
        sys.exit(1)

    lan_ip = local_ip()
    logger.info(f"Abre el panel local en http://127.0.0.1:{PORT}")
    if lan_ip:
        logger.info(f"Desde tu celular en la misma WiFi: http://{lan_ip}:{PORT}")
    uvicorn.run("application.webapp:app", host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
