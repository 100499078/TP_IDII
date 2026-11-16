"""Reglas de alerta en PostgreSQL (tabla regla_alerta, fase 9).

Son CONFIGURACIÓN: pocas filas, se leen mucho y se cambian poco. Por eso van en la base
relacional (con CHECK que impiden umbrales absurdos) y la telemetría las guarda en memoria
60 segundos para no consultarlas en cada lectura.
"""
from __future__ import annotations

from app.dominio.modelos import ReglaAlerta
from app.infra.conexiones import GestorConexiones


def _a_regla(f: dict) -> ReglaAlerta:
    return ReglaAlerta(f["tipo"], float(f["umbral"]) if f["umbral"] is not None else None, f["unidad"],
                       f["activa"], f["descripcion"], f["fecha_modificacion"])


class RepositorioReglas:
    def __init__(self, conexiones: GestorConexiones):
        self._cx = conexiones

    def listar(self) -> list[ReglaAlerta]:
        with self._cx.transaccion() as cur:
            cur.execute("SELECT * FROM regla_alerta ORDER BY tipo")
            return [_a_regla(f) for f in cur.fetchall()]

    def actualizar(self, tipo: str, umbral: float | None, activa: bool) -> bool:
        with self._cx.transaccion() as cur:
            cur.execute("""UPDATE regla_alerta SET umbral = %s, activa = %s, fecha_modificacion = now()
                            WHERE tipo = %s""", (umbral, activa, tipo))
            return cur.rowcount == 1
