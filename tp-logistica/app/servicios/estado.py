"""Estado de los cinco motores (para el menú y para el arranque)."""
from __future__ import annotations

from app.infra.conexiones import EstadoMotor, GestorConexiones


class ServicioEstado:
    def __init__(self, conexiones: GestorConexiones):
        self._cx = conexiones

    def verificar(self) -> list[EstadoMotor]:
        return self._cx.verificar()
