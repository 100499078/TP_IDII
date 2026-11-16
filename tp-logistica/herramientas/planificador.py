"""Planificador de procesos periódicos (fase 7).

Uso:  python -m herramientas.planificador                 (corre hasta Ctrl+C; cada proceso cuando le toca)
      python -m herramientas.planificador --listar        (estado de cada proceso)
      python -m herramientas.planificador --proceso riesgo  (corre uno ahora y termina)

Cada 5 segundos pregunta qué procesos ya cumplieron su intervalo (la última ejecución está
en Redis) y los corre. Si se abren dos planificadores, el lock de Redis de cada proceso
evita que el mismo proceso corra dos veces a la vez.
"""
from __future__ import annotations

import argparse
import sys
import time

from rich.console import Console

from app.__main__ import construir_procesos
from app.config import Config
from app.infra.conexiones import GestorConexiones
from app.logs import configurar_logging

consola = Console()


def _mostrar(r) -> None:
    color = "green" if r.ok else "red"
    consola.print(f"{time.strftime('%H:%M:%S')}  [{color}]{r.nombre:<9}[/{color}] {r.resultado}  [dim]({r.segundos} s)[/dim]")


def main() -> int:
    parser = argparse.ArgumentParser(description="Corre los procesos periódicos del sistema")
    grupo = parser.add_mutually_exclusive_group()
    grupo.add_argument("--listar", action="store_true")
    grupo.add_argument("--proceso", metavar="NOMBRE")
    args = parser.parse_args()

    cfg = Config.desde_entorno()
    configurar_logging(cfg.log_carpeta, cfg.log_nivel)
    cx = GestorConexiones(cfg)
    procesos = construir_procesos(cx, cfg).procesos
    try:
        if args.listar:
            for f in procesos.estado():
                u = f["ultimo"]
                consola.print(f"{f['proceso'].nombre:<9} {f['proceso'].descripcion:<58} "
                              f"última: {u['inicio']:%Y-%m-%d %H:%M} {'OK' if u['ok'] else 'ERROR'}" if u else
                              f"{f['proceso'].nombre:<9} {f['proceso'].descripcion:<58} última: nunca")
            return 0
        if args.proceso:
            r = procesos.ejecutar(args.proceso)
            _mostrar(r)
            return 0 if r.ok else 1
        consola.print("Planificador en marcha: revisa cada 5 s qué procesos tienen que correr (Ctrl+C para terminar)")
        while True:
            try:
                for nombre in procesos.pendientes():
                    r = procesos.ejecutar(nombre)
                    if nombre != "reportes" or not r.resultado.startswith("0 reportes generados, 0"):
                        _mostrar(r)                      # el de reportes solo se muestra si hizo algo
            except Exception as exc:  # noqa: BLE001   (p. ej. Redis caído: se reintenta en la próxima vuelta)
                consola.print(f"[red]{time.strftime('%H:%M:%S')}  No se pudo revisar los procesos: {exc}[/red]")
            time.sleep(5)
    except KeyboardInterrupt:
        return 0
    finally:
        cx.cerrar()


if __name__ == "__main__":
    sys.exit(main())
