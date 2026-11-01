"""Contenedores y sensores IoT (catálogo en PostgreSQL; las lecturas van a Cassandra)."""
from __future__ import annotations

from datetime import date

import psycopg

from app.dominio.modelos import ESTADOS_ENVIO_EN_CURSO, Contenedor, Sensor
from app.excepciones import ErrorConflicto
from app.infra.conexiones import GestorConexiones
from app.repositorios.postgres.outbox import registrar_evento


def _evento_contenedor(cur, c: Contenedor) -> None:
    # El estado no viaja al grafo: cambia seguido y para recorrer rutas no hace falta
    registrar_evento(cur, "contenedor", c.id_contenedor, "CONTENEDOR_GUARDADO",
                     {"id_contenedor": c.id_contenedor, "codigo": c.codigo_internacional, "tipo": c.tipo})


class RepositorioContenedores:
    def __init__(self, conexiones: GestorConexiones):
        self._cx = conexiones

    # ------------------------------------------------------------------ contenedores
    def crear_contenedor(self, c: Contenedor) -> int:
        try:
            with self._cx.transaccion() as cur:
                cur.execute(
                    """INSERT INTO contenedor (codigo_internacional, tipo, capacidad_m3)
                       VALUES (%s, %s, %s) RETURNING id_contenedor""",
                    (c.codigo_internacional, c.tipo, c.capacidad_m3))
                c.id_contenedor = cur.fetchone()["id_contenedor"]
                _evento_contenedor(cur, c)
                return c.id_contenedor
        except psycopg.errors.UniqueViolation as exc:
            raise ErrorConflicto(f"Ya existe el contenedor {c.codigo_internacional}") from exc

    def cambiar_estado_contenedor(self, id_contenedor: int, estado: str) -> None:
        with self._cx.transaccion() as cur:
            cur.execute("UPDATE contenedor SET estado = %s WHERE id_contenedor = %s", (estado, id_contenedor))

    def contenedor(self, id_contenedor: int) -> Contenedor | None:
        lista = self.listar_contenedores(id_contenedor=id_contenedor)
        return lista[0] if lista else None

    def listar_contenedores(self, estado: str | None = None, id_contenedor: int | None = None) -> list[Contenedor]:
        sql = """SELECT c.*, count(s.id_sensor) AS sensores FROM contenedor c
                 LEFT JOIN sensor s ON s.id_contenedor = c.id_contenedor AND s.estado <> 'BAJA'"""
        params: list = []
        if estado:
            sql += " WHERE c.estado = %s"
            params.append(estado)
        elif id_contenedor is not None:
            sql += " WHERE c.id_contenedor = %s"
            params.append(id_contenedor)
        with self._cx.transaccion() as cur:
            cur.execute(sql + " GROUP BY c.id_contenedor ORDER BY c.codigo_internacional", params)
            return [Contenedor(f["id_contenedor"], f["codigo_internacional"], f["tipo"], float(f["capacidad_m3"]),
                               f["estado"], f["sensores"]) for f in cur.fetchall()]

    # ------------------------------------------------------------------ sensores
    def crear_sensor(self, s: Sensor) -> int:
        with self._cx.transaccion() as cur:
            cur.execute(
                """INSERT INTO sensor (tipo, fabricante, modelo, temp_min, temp_max)
                   VALUES (%s, %s, %s, %s, %s) RETURNING id_sensor""",
                (s.tipo, s.fabricante, s.modelo, s.temp_min, s.temp_max))
            s.id_sensor = cur.fetchone()["id_sensor"]
            return s.id_sensor

    def instalar_sensor(self, id_sensor: int, id_contenedor: int | None, fecha: date | None) -> None:
        """Asocia (o desasocia, con None) un sensor a un contenedor."""
        with self._cx.transaccion() as cur:
            cur.execute("UPDATE sensor SET id_contenedor = %s, fecha_instalacion = %s WHERE id_sensor = %s",
                        (id_contenedor, fecha, id_sensor))

    def cambiar_estado_sensor(self, id_sensor: int, estado: str) -> None:
        with self._cx.transaccion() as cur:
            cur.execute("UPDATE sensor SET estado = %s WHERE id_sensor = %s", (estado, id_sensor))

    def sensor(self, id_sensor: int) -> Sensor | None:
        lista = self.listar_sensores(id_sensor=id_sensor)
        return lista[0] if lista else None

    def listar_sensores(self, id_contenedor: int | None = None, id_sensor: int | None = None) -> list[Sensor]:
        sql = """SELECT s.*, c.codigo_internacional AS codigo_contenedor FROM sensor s
                 LEFT JOIN contenedor c ON c.id_contenedor = s.id_contenedor"""
        params: list = []
        if id_contenedor is not None:
            sql += " WHERE s.id_contenedor = %s"
            params.append(id_contenedor)
        elif id_sensor is not None:
            sql += " WHERE s.id_sensor = %s"
            params.append(id_sensor)
        with self._cx.transaccion() as cur:
            cur.execute(sql + " ORDER BY s.id_sensor", params)
            return [Sensor(id_sensor=f["id_sensor"], tipo=f["tipo"], fabricante=f["fabricante"], modelo=f["modelo"],
                           estado=f["estado"], id_contenedor=f["id_contenedor"],
                           fecha_instalacion=f["fecha_instalacion"],
                           temp_min=float(f["temp_min"]) if f["temp_min"] is not None else None,
                           temp_max=float(f["temp_max"]) if f["temp_max"] is not None else None,
                           codigo_contenedor=f["codigo_contenedor"]) for f in cur.fetchall()]

    # ------------------------------------------------------------------ para la telemetría (fase 3)
    def sensores_instalados_activos(self) -> list[Sensor]:
        """Sensores ACTIVOS que están instalados en un contenedor: son los que mandan lecturas."""
        return [s for s in self.listar_sensores() if s.estado == "ACTIVO" and s.id_contenedor is not None]

    def envio_en_curso_de_contenedor(self, id_contenedor: int) -> int | None:
        """El envío en curso que lleva el contenedor (None si está libre)."""
        with self._cx.transaccion() as cur:
            cur.execute(
                """SELECT e.id_envio FROM envio_contenedor ec JOIN envio e USING (id_envio)
                    WHERE ec.id_contenedor = %s AND e.estado = ANY(%s)
                    ORDER BY e.fecha_creacion DESC LIMIT 1""", (id_contenedor, list(ESTADOS_ENVIO_EN_CURSO)))
            fila = cur.fetchone()
        return fila["id_envio"] if fila else None
