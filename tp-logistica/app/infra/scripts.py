"""Lectura de los scripts de creación (.cypher, .cql) para ejecutarlos desde Python."""
from __future__ import annotations

from pathlib import Path


def dividir_sentencias(texto: str, comentario: str) -> list[str]:
    """Separa un script en sentencias terminadas en ';' y descarta comentarios
    de línea completa o al final de la línea (`//` en Cypher, `--` en CQL)."""
    lineas = []
    for linea in texto.splitlines():
        if comentario in linea:
            linea = linea.split(comentario, 1)[0]
        if linea.strip():
            lineas.append(linea.rstrip())
    sentencias = [s.strip() for s in "\n".join(lineas).split(";")]
    return [s for s in sentencias if s]


def leer_sentencias(ruta: Path) -> list[str]:
    comentario = {".cypher": "//", ".cql": "--"}[ruta.suffix]
    return dividir_sentencias(ruta.read_text(encoding="utf-8"), comentario)
