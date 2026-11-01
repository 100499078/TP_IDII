"""Cola de sincronización en un Redis Stream (sync:stream) con grupo de consumidores.

- XADD agrega el evento al final del stream.
- XREADGROUP entrega cada evento a un consumidor del grupo y lo deja "pendiente"
  hasta que el consumidor confirma con XACK. Si el consumidor falla antes de
  confirmar, el evento sigue pendiente y se vuelve a entregar.
- Los eventos que fallan muchas veces se mueven a sync:fallidos (cola de
  mensajes muertos) para no trabar la sincronización.
- Puede haber VARIOS grupos sobre el mismo stream: cada grupo recibe TODOS los
  eventos y lleva su propia posición. Neo4j lee con el grupo "sincronizadores" y la
  trazabilidad de MongoDB con el grupo "trazabilidad" (fase 4): si uno se atrasa,
  el otro sigue.
"""
from __future__ import annotations

import json

from app.infra.conexiones import GestorConexiones

STREAM = "sync:stream"
GRUPO = "sincronizadores"            # el de Neo4j
GRUPO_TRAZABILIDAD = "trazabilidad"   # el de MongoDB (eventos logísticos)
FALLIDOS = "sync:fallidos"


class RepositorioColaSync:
    def __init__(self, conexiones: GestorConexiones, grupo: str = GRUPO):
        self._cx = conexiones
        self.grupo = grupo

    def asegurar_grupo(self) -> None:
        import redis
        try:
            # id="0": un grupo nuevo empieza desde el principio del stream (recibe también lo anterior)
            self._cx.redis.xgroup_create(STREAM, self.grupo, id="0", mkstream=True)
        except redis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):     # el grupo ya existía
                raise

    def publicar(self, eventos: list[dict]) -> None:
        with self._cx.redis.pipeline(transaction=True) as p:
            for e in eventos:
                p.xadd(STREAM, {"id_evento": e["id_evento"], "tipo_evento": e["tipo_evento"],
                                "payload": json.dumps(e["payload"])},
                       maxlen=100_000, approximate=True)
            p.execute()

    def leer(self, consumidor: str, cantidad: int = 100) -> list[tuple[str, dict | None]]:
        """Primero los pendientes de este consumidor (ya entregados y sin confirmar),
        después los nuevos. Devuelve [(id_mensaje, campos), ...] en orden."""
        r = self._cx.redis
        respuesta = r.xreadgroup(self.grupo, consumidor, {STREAM: "0"}, count=cantidad)
        mensajes = respuesta[0][1] if respuesta else []
        if not mensajes:
            respuesta = r.xreadgroup(self.grupo, consumidor, {STREAM: ">"}, count=cantidad)
            mensajes = respuesta[0][1] if respuesta else []
        return [(id_msg, campos or None) for id_msg, campos in mensajes]

    def entregas(self, id_mensaje: str) -> int:
        info = self._cx.redis.xpending_range(STREAM, self.grupo, min=id_mensaje, max=id_mensaje, count=1)
        return int(info[0]["times_delivered"]) if info else 0

    def confirmar(self, id_mensaje: str) -> None:
        self._cx.redis.xack(STREAM, self.grupo, id_mensaje)

    def mover_a_fallidos(self, id_mensaje: str, campos: dict, error: str) -> None:
        with self._cx.redis.pipeline(transaction=True) as p:
            p.xadd(FALLIDOS, {**campos, "error": error[:500], "id_original": id_mensaje, "grupo": self.grupo})
            p.xack(STREAM, self.grupo, id_mensaje)
            p.execute()

    def estado(self) -> dict:
        r = self._cx.redis
        try:
            pendientes = r.xpending(STREAM, self.grupo)["pending"] if r.exists(STREAM) else 0
        except Exception:  # noqa: BLE001   (el grupo todavía no existe)
            pendientes = 0
        return {"en_stream": r.xlen(STREAM) if r.exists(STREAM) else 0,
                "sin_confirmar": pendientes,
                "fallidos": r.xlen(FALLIDOS) if r.exists(FALLIDOS) else 0}
