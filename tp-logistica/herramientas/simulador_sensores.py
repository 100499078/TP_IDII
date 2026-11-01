"""Simulador de sensores IoT: manda lecturas como si fueran sensores reales.

Uso:  python -m herramientas.simulador_sensores                    (en vivo, cada 5 s, hasta Ctrl+C)
      python -m herramientas.simulador_sensores --intervalo 2      (más rápido)
      python -m herramientas.simulador_sensores --anomalias 0.2    (20 % de lecturas fuera de rango)
      python -m herramientas.simulador_sensores --lote 24 --cada 10 (24 h de historial, una cada 10 min)

Usa todos los sensores ACTIVOS instalados en un contenedor. Conviene dejarlo
corriendo en una segunda terminal mientras se usa la aplicación: las alertas
aparecen en el menú principal de los operadores.
"""
from __future__ import annotations

import argparse
import sys
import time

from rich.console import Console

from app.__main__ import construir_telemetria
from app.config import Config
from app.infra.conexiones import GestorConexiones
from app.logs import configurar_logging

consola = Console()


def _mostrar(sensor, momento, valores, resultado) -> None:
    datos = "  ".join(f"{k}={v}" for k, v in valores.items())
    if isinstance(resultado, Exception):
        consola.print(f"[red]{momento:%H:%M:%S} sensor {sensor.id_sensor}: rechazada ({resultado})[/red]")
        return
    consola.print(f"{momento:%H:%M:%S} sensor {sensor.id_sensor} (contenedor {sensor.id_contenedor}): {datos}")
    for a in resultado:
        consola.print(f"   [bold red]⚠ {a.tipo}: {a.mensaje}[/bold red]")


def main() -> int:
    parser = argparse.ArgumentParser(description="Simula lecturas de los sensores instalados")
    parser.add_argument("--intervalo", type=float, default=5, help="segundos entre rondas (modo en vivo)")
    parser.add_argument("--anomalias", type=float, default=0.05, help="probabilidad de lectura fuera de rango")
    parser.add_argument("--lote", type=int, metavar="HORAS", help="genera historial de HORAS hacia atrás y termina")
    parser.add_argument("--cada", type=int, default=10, help="minutos entre lecturas del lote")
    args = parser.parse_args()

    cfg = Config.desde_entorno()
    configurar_logging(cfg.log_carpeta, cfg.log_nivel)
    cx = GestorConexiones(cfg)
    _, simulador = construir_telemetria(cx)
    simulador.prob_anomalia = args.anomalias
    try:
        if args.lote:
            with consola.status(f"Generando {args.lote} h de lecturas..."):
                r = simulador.lote(args.lote, args.cada)
            consola.print(f"[green]{r.lecturas} lecturas, {len(r.alertas)} alertas, {r.errores} rechazadas[/green]")
            return 0
        consola.print(f"Simulando sensores cada {args.intervalo:g} s (Ctrl+C para terminar)...")
        while True:
            r = simulador.ahora(_mostrar)
            if r.lecturas == 0 and r.errores == 0:
                consola.print("[yellow]No hay sensores ACTIVOS instalados en contenedores.[/yellow]")
            time.sleep(args.intervalo)
    except KeyboardInterrupt:
        return 0
    finally:
        cx.cerrar()


if __name__ == "__main__":
    sys.exit(main())
