"""Estado de los procesos periódicos en Redis.

    proceso:{nombre}  HASH  inicio, fin, ok (1/0), resultado, segundos

Es lo que el planificador mira para saber si a un proceso "ya le toca" otra vez, y lo que
muestra el menú. Si Redis pierde esta clave, lo único que pasa es que el proceso corre en la
próxima vuelta (todos son idempotentes).
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.infra.conexiones import GestorConexiones


class RepositorioEstadoProcesos:
    def __init__(self, conexiones: GestorConexiones):
        self._cx = conexiones

    def registrar(self, nombre: str, inicio: datetime, fin: datetime, ok: bool, resultado: str) -> None:
        self._cx.redis.hset(f"proceso:{nombre}", mapping={
            "inicio": inicio.timestamp(), "fin": fin.timestamp(), "ok": int(ok), "resultado": resultado[:300],
            "segundos": round((fin - inicio).total_seconds(), 2)})

    def ultimo(self, nombre: str) -> dict | None:
        d = self._cx.redis.hgetall(f"proceso:{nombre}")
        if not d:
            return None
        return {"inicio": datetime.fromtimestamp(float(d["inicio"]), timezone.utc),
                "fin": datetime.fromtimestamp(float(d["fin"]), timezone.utc), "ok": d["ok"] == "1",
                "resultado": d["resultado"], "segundos": float(d["segundos"])}
