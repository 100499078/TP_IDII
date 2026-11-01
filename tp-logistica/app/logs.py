"""Registro de logs.

- Archivo logs/app.log con TODO (nivel configurable, por defecto INFO), rotado
  cada 1 MB y con 5 copias, para que no crezca sin límite.
- Archivo logs/errores.log solo con WARNING y ERROR (con el detalle técnico).
- En la consola no se imprime nada: la consola es la interfaz del usuario y los
  errores se le muestran con un mensaje claro desde la capa de presentación.
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

FORMATO = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


def configurar_logging(carpeta: Path, nivel: str = "INFO") -> None:
    carpeta.mkdir(parents=True, exist_ok=True)
    raiz = logging.getLogger()
    raiz.setLevel(nivel)
    for h in list(raiz.handlers):          # evita duplicar handlers si se llama dos veces
        raiz.removeHandler(h)

    formato = logging.Formatter(FORMATO)

    general = RotatingFileHandler(carpeta / "app.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    general.setLevel(nivel)
    general.setFormatter(formato)

    errores = RotatingFileHandler(carpeta / "errores.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    errores.setLevel(logging.WARNING)
    errores.setFormatter(formato)

    raiz.addHandler(general)
    raiz.addHandler(errores)

    # Los drivers son muy verbosos en DEBUG/INFO: solo nos interesan sus advertencias.
    for ruidoso in ("cassandra", "neo4j", "pymongo", "urllib3"):
        logging.getLogger(ruidoso).setLevel(logging.WARNING)
