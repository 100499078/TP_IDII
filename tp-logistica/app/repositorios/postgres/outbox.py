"""Tabla outbox: eventos pendientes de propagar a Neo4j (patrón Transactional Outbox).

El evento se inserta con el MISMO cursor (misma transacción) que el cambio del
dato. Si la transacción hace ROLLBACK, el evento también desaparece; si hace
COMMIT, el evento queda guardado aunque Neo4j o Redis estén caídos. Así nunca
se pierde un cambio ni se propaga uno que no ocurrió.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from psycopg.types.json import Jsonb

from app.infra.conexiones import GestorConexiones


def _a_json(valor):
    """Convierte fechas y decimales a tipos que JSON entiende."""
    if isinstance(valor, (datetime, date)):
        return valor.isoformat()
    if isinstance(valor, Decimal):
        return float(valor)
    if isinstance(valor, dict):
        return {k: _a_json(v) for k, v in valor.items()}
    if isinstance(valor, (list, tuple)):
        return [_a_json(v) for v in valor]
    return valor


def registrar_evento(cur, agregado: str, id_agregado: int, tipo_evento: str, payload: dict) -> None:
    """Se llama DENTRO de la transacción del repositorio que modifica el dato."""
    cur.execute(
        """INSERT INTO outbox (agregado, id_agregado, tipo_evento, payload)
           VALUES (%s, %s, %s, %s)""",
        (agregado, id_agregado, tipo_evento, Jsonb(_a_json(payload))))


class RepositorioOutbox:
    def __init__(self, conexiones: GestorConexiones):
        self._cx = conexiones

    def pendientes(self, limite: int = 200) -> list[dict]:
        with self._cx.transaccion() as cur:
            cur.execute(
                """SELECT id_evento, agregado, id_agregado, tipo_evento, payload
                     FROM outbox WHERE procesado_en IS NULL
                    ORDER BY id_evento LIMIT %s""", (limite,))
            return cur.fetchall()

    def marcar_publicados(self, ids: list[int]) -> None:
        if not ids:
            return
        with self._cx.transaccion() as cur:
            cur.execute("UPDATE outbox SET procesado_en = now() WHERE id_evento = ANY(%s)", (ids,))

    def contar_pendientes(self) -> int:
        with self._cx.transaccion() as cur:
            cur.execute("SELECT count(*) AS n FROM outbox WHERE procesado_en IS NULL")
            return cur.fetchone()["n"]
