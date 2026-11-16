"""Verificador de consistencia entre los cinco motores (fase 8).

Uso:  python -m herramientas.verificar

Compara lo que dice cada motor sobre los mismos hechos (envíos en PostgreSQL, Neo4j y MongoDB;
incidentes en MongoDB y Neo4j; último contacto de sensores en Cassandra y Redis; sesiones en
PostgreSQL y Redis) y controla PostgreSQL por dentro (saldos, facturas, cierres, contenedores).
Solo lee: no corrige nada. Termina con código 1 si encontró algún ERROR (sirve para un script).
"""
from __future__ import annotations

import sys

from app.__main__ import construir_verificacion
from app.config import Config
from app.infra.conexiones import GestorConexiones
from app.logs import configurar_logging
from app.presentacion.menus.sistema import mostrar_controles
from app.servicios.verificacion import ERROR


def main() -> int:
    cfg = Config.desde_entorno()
    configurar_logging(cfg.log_carpeta, cfg.log_nivel)
    cx = GestorConexiones(cfg)
    try:
        controles = construir_verificacion(cx).verificar()
        mostrar_controles(controles)
        return 1 if any(c.estado == ERROR for c in controles) else 0
    finally:
        cx.cerrar()


if __name__ == "__main__":
    sys.exit(main())
