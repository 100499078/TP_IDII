"""Sesiones activas en Redis.

Claves (ver el modelo de Redis en el punto 2):
  sesion:{id_sesion}             hash con los datos de la sesión, con TTL
  usuario:{id_usuario}:sesiones  set con las sesiones activas del usuario
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.dominio.modelos import Sesion
from app.infra.conexiones import GestorConexiones


def _clave(id_sesion: str) -> str:
    return f"sesion:{id_sesion}"


def _clave_usuario(id_usuario: int) -> str:
    return f"usuario:{id_usuario}:sesiones"


class RepositorioSesionesActivas:
    def __init__(self, conexiones: GestorConexiones):
        self._cx = conexiones

    def guardar(self, sesion: Sesion, ttl_segundos: int) -> None:
        """HSET + EXPIRE + SADD dentro de MULTI/EXEC: Redis los aplica juntos."""
        r = self._cx.redis
        with r.pipeline(transaction=True) as p:
            p.hset(_clave(sesion.id_sesion), mapping={
                "id_usuario": sesion.id_usuario,
                "email": sesion.email,
                "nombre": sesion.nombre,
                "roles": ",".join(sesion.roles),
                "nivel_permisos": sesion.nivel_permisos,
                "fecha_inicio": int(sesion.fecha_inicio.timestamp()),
                "ultimo_acceso": int(sesion.ultimo_acceso.timestamp()),
                "id_actor": sesion.id_actor if sesion.id_actor is not None else "",
            })
            p.expire(_clave(sesion.id_sesion), ttl_segundos)
            p.sadd(_clave_usuario(sesion.id_usuario), sesion.id_sesion)
            p.execute()

    def obtener(self, id_sesion: str) -> Sesion | None:
        datos = self._cx.redis.hgetall(_clave(id_sesion))
        if not datos:
            return None
        return Sesion(
            id_sesion=id_sesion,
            id_usuario=int(datos["id_usuario"]),
            email=datos["email"],
            nombre=datos["nombre"],
            roles=[x for x in datos.get("roles", "").split(",") if x],
            nivel_permisos=int(datos["nivel_permisos"]),
            fecha_inicio=datetime.fromtimestamp(int(datos["fecha_inicio"]), tz=timezone.utc),
            ultimo_acceso=datetime.fromtimestamp(int(datos["ultimo_acceso"]), tz=timezone.utc),
            id_actor=int(datos["id_actor"]) if datos.get("id_actor") else None,
        )

    def renovar(self, id_sesion: str, ttl_segundos: int, ahora: datetime) -> None:
        r = self._cx.redis
        with r.pipeline(transaction=True) as p:
            p.hset(_clave(id_sesion), "ultimo_acceso", int(ahora.timestamp()))
            p.expire(_clave(id_sesion), ttl_segundos)
            p.execute()

    def segundos_restantes(self, id_sesion: str) -> int:
        return int(self._cx.redis.ttl(_clave(id_sesion)))

    def eliminar(self, id_sesion: str, id_usuario: int) -> None:
        r = self._cx.redis
        with r.pipeline(transaction=True) as p:
            p.delete(_clave(id_sesion))
            p.srem(_clave_usuario(id_usuario), id_sesion)
            p.execute()

    def de_usuario(self, id_usuario: int) -> list[str]:
        return sorted(self._cx.redis.smembers(_clave_usuario(id_usuario)))

    def existe(self, id_sesion: str) -> bool:
        return bool(self._cx.redis.exists(_clave(id_sesion)))
