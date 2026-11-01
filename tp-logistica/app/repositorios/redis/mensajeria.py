"""Redis en la mensajería.

- noleidos:{id_usuario}         HASH  campo = id_conversacion, valor = mensajes sin leer (HINCRBY).
                                Es una copia rápida de lo que se puede contar en MongoDB: si la
                                clave no existe, se recalcula desde MongoDB (como una caché).
- limite:mensajes:{id_usuario}  STRING con TTL de 60 s: INCR por cada mensaje (limitador de
                                frecuencia de ventana fija, para evitar spam).
- usuario:{id}:sesiones         SET de la fase 1: si alguna de esas sesiones sigue viva, el
                                usuario está "en línea".
"""
from __future__ import annotations

from app.infra.conexiones import GestorConexiones

MENSAJES_POR_MINUTO = 20
VACIO = "_"            # marca de "hash calculado y sin pendientes" (un hash vacío no existe en Redis)


class RepositorioMensajeriaRedis:
    def __init__(self, conexiones: GestorConexiones):
        self._cx = conexiones

    # ------------------------------------------------------------------ no leídos
    def sumar_no_leidos(self, ids_usuario: list[int], id_conversacion: str) -> None:
        with self._cx.redis.pipeline(transaction=False) as p:
            for u in ids_usuario:
                p.hincrby(f"noleidos:{u}", id_conversacion, 1)
            p.execute()

    def no_leidos(self, id_usuario: int) -> dict[str, int] | None:
        """{id_conversacion: cantidad}, o None si la clave no existe (hay que recalcular)."""
        datos = self._cx.redis.hgetall(f"noleidos:{id_usuario}")
        if not datos:
            return None
        return {k: int(v) for k, v in datos.items() if k != VACIO and int(v) > 0}

    def reemplazar_no_leidos(self, id_usuario: int, conteos: dict[str, int]) -> None:
        clave = f"noleidos:{id_usuario}"
        with self._cx.redis.pipeline(transaction=True) as p:      # MULTI/EXEC: se ve todo o nada
            p.delete(clave)
            p.hset(clave, mapping={VACIO: 0, **conteos})
            p.execute()

    def limpiar(self, id_usuario: int, id_conversacion: str) -> None:
        self._cx.redis.hdel(f"noleidos:{id_usuario}", id_conversacion)

    # ------------------------------------------------------------------ limitador de frecuencia
    def permitir_envio(self, id_usuario: int) -> bool:
        clave = f"limite:mensajes:{id_usuario}"
        with self._cx.redis.pipeline(transaction=True) as p:
            p.incr(clave)
            p.expire(clave, 60, nx=True)          # el TTL se pone solo con el primer mensaje del minuto
            cantidad, _ = p.execute()
        return cantidad <= MENSAJES_POR_MINUTO

    # ------------------------------------------------------------------ presencia
    def en_linea(self, ids_usuario: list[int]) -> set[int]:
        r = self._cx.redis
        conectados = set()
        for u in ids_usuario:
            sesiones = r.smembers(f"usuario:{u}:sesiones")
            if sesiones and r.exists(*[f"sesion:{s}" for s in sesiones]):
                conectados.add(u)
        return conectados
