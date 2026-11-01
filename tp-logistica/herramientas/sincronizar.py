"""Sincronizador PostgreSQL → Redis → Neo4j como proceso independiente.

Uso:  python -m herramientas.sincronizar               (corre hasta Ctrl+C)
      python -m herramientas.sincronizar --una-vez     (un solo ciclo)
      python -m herramientas.sincronizar --estado      (muestra cómo está la cola)
      python -m herramientas.sincronizar --reconstruir (rearma el grafo desde PostgreSQL)

La aplicación de consola ya lo ejecuta en un hilo de fondo; este comando sirve
para correrlo aparte (por ejemplo, con la aplicación lanzada con SINCRONIZADOR=0)
o para demostrar en la defensa qué pasa en cada paso.
"""
from __future__ import annotations

import argparse
import sys
import time

from rich.console import Console

from app.__main__ import construir_sincronizacion
from app.config import Config
from app.infra.conexiones import GestorConexiones
from app.logs import configurar_logging

consola = Console()


def main() -> int:
    parser = argparse.ArgumentParser(description="Sincroniza el grafo de Neo4j con PostgreSQL")
    grupo = parser.add_mutually_exclusive_group()
    grupo.add_argument("--una-vez", action="store_true")
    grupo.add_argument("--estado", action="store_true")
    grupo.add_argument("--reconstruir", action="store_true")
    args = parser.parse_args()

    cfg = Config.desde_entorno()
    configurar_logging(cfg.log_carpeta, cfg.log_nivel)
    cx = GestorConexiones(cfg)
    servicio = construir_sincronizacion(cx)
    try:
        if args.estado:
            for clave, valor in servicio.estado().items():
                consola.print(f"{clave:>20}: {valor}")
        elif args.reconstruir:
            consola.print(f"[green]Grafo reconstruido con {servicio.reconstruir_grafo()} eventos[/green]")
        elif args.una_vez:
            publicados, aplicados = servicio.ciclo()
            consola.print(f"{publicados} eventos publicados en Redis, {aplicados} aplicados en Neo4j")
        else:
            consola.print("Sincronizando cada 3 segundos (Ctrl+C para terminar)...")
            while True:
                publicados, aplicados = servicio.ciclo()
                if publicados or aplicados:
                    consola.print(f"{time.strftime('%H:%M:%S')}  publicados={publicados}  aplicados={aplicados}")
                time.sleep(3)
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        cx.cerrar()


if __name__ == "__main__":
    sys.exit(main())
