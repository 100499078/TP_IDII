"""Caché de consultas caras en Redis (patrón cache-aside).

    1. Se busca en Redis.  2. Si no está, se calcula en la base y se guarda con TTL.
    3. Cuando el dato cambia, se BORRA la clave (invalidación): la próxima consulta recalcula.
"""
from __future__ import annotations

import json

from app.infra.conexiones import GestorConexiones


class RepositorioCache:
    def __init__(self, conexiones: GestorConexiones):
        self._cx = conexiones

    def obtener(self, clave: str):
        valor = self._cx.redis.get(clave)
        return None if valor is None else json.loads(valor)

    def guardar(self, clave: str, valor, segundos: int) -> None:
        self._cx.redis.set(clave, json.dumps(valor, default=str), ex=segundos)

    def borrar(self, clave: str) -> None:
        self._cx.redis.delete(clave)

    def segundos_restantes(self, clave: str) -> int:
        return self._cx.redis.ttl(clave)
