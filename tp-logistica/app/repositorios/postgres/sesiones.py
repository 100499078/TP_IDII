"""Auditoría de sesiones en PostgreSQL (tabla sesion_historial).

La sesión "viva" está en Redis; acá queda el registro permanente: cuándo
empezó, cuándo terminó y cómo (CERRADA, EXPIRADA o REVOCADA).
"""
from __future__ import annotations

from app.infra.conexiones import GestorConexiones

ESTADOS_FINALES = ("CERRADA", "EXPIRADA", "REVOCADA")


class RepositorioHistorialSesiones:
    def __init__(self, conexiones: GestorConexiones):
        self._cx = conexiones

    def registrar_inicio(self, id_sesion: str, id_usuario: int) -> None:
        with self._cx.transaccion() as cur:
            cur.execute("INSERT INTO sesion_historial (id_sesion, id_usuario) VALUES (%s, %s)",
                        (id_sesion, id_usuario))

    def finalizar(self, id_sesion: str, estado: str) -> bool:
        """Marca la sesión como terminada. Devuelve False si ya estaba terminada
        (así llamar dos veces no pisa el primer cierre)."""
        if estado not in ESTADOS_FINALES:
            raise ValueError(f"Estado final inválido: {estado}")
        with self._cx.transaccion() as cur:
            cur.execute(
                """UPDATE sesion_historial SET estado = %s, fecha_fin = now()
                    WHERE id_sesion = %s AND estado = 'ACTIVA'""", (estado, id_sesion))
            return cur.rowcount == 1

    def activas(self) -> list[dict]:
        with self._cx.transaccion() as cur:
            cur.execute(
                """SELECT s.id_sesion::text AS id_sesion, s.id_usuario, u.email, s.fecha_inicio
                     FROM sesion_historial s JOIN usuario u USING (id_usuario)
                    WHERE s.estado = 'ACTIVA' ORDER BY s.fecha_inicio""")
            return cur.fetchall()
