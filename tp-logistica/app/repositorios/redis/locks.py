"""Locks distribuidos en Redis: que un proceso no corra dos veces a la vez.

    lock:proceso:{nombre}  STRING con el "dueño" (un id único) y vencimiento (SET NX EX)

- Tomar: SET clave dueño NX EX segundos -> solo uno lo consigue (es atómico).
- El vencimiento evita que el lock quede tomado para siempre si el proceso se cae.
- Soltar: solo si el lock sigue siendo MÍO. Leer y borrar en dos pasos no sirve (entre
  medio pudo vencer y tomarlo otro), así que se hace con un script Lua, que Redis ejecuta
  de una vez, sin que nada se intercale.
"""
from __future__ import annotations

import uuid

from app.infra.conexiones import GestorConexiones

SOLTAR_SI_ES_MIO = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


class RepositorioLocks:
    def __init__(self, conexiones: GestorConexiones):
        self._cx = conexiones

    def tomar(self, nombre: str, segundos: int) -> str | None:
        """Devuelve el token del dueño si consiguió el lock, o None si otro lo tiene."""
        token = uuid.uuid4().hex
        ok = self._cx.redis.set(f"lock:proceso:{nombre}", token, nx=True, ex=segundos)
        return token if ok else None

    def soltar(self, nombre: str, token: str) -> bool:
        return bool(self._cx.redis.eval(SOLTAR_SI_ES_MIO, 1, f"lock:proceso:{nombre}", token))

    def segundos_restantes(self, nombre: str) -> int:
        return self._cx.redis.ttl(f"lock:proceso:{nombre}")
