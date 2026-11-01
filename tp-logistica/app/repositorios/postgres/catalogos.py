"""Catálogos: ubicaciones, vehículos y tarifas."""
from __future__ import annotations

from datetime import date

import psycopg

from app.dominio.modelos import Tarifa, Ubicacion, Vehiculo
from app.excepciones import ErrorConflicto
from app.infra.conexiones import GestorConexiones
from app.repositorios.postgres.outbox import registrar_evento


def _evento_ubicacion(cur, u: Ubicacion) -> None:
    registrar_evento(cur, "ubicacion", u.id_ubicacion, "UBICACION_GUARDADA",
                     {"codigo": u.codigo, "nombre": u.nombre, "tipo": u.tipo, "pais": u.pais,
                      "latitud": u.latitud, "longitud": u.longitud})


def _a_ubicacion(f: dict) -> Ubicacion:
    return Ubicacion(id_ubicacion=f["id_ubicacion"], codigo=f["codigo"], nombre=f["nombre"], tipo=f["tipo"],
                     pais=f["pais"], latitud=float(f["latitud"]), longitud=float(f["longitud"]))


class RepositorioCatalogos:
    def __init__(self, conexiones: GestorConexiones):
        self._cx = conexiones

    # ------------------------------------------------------------------ ubicaciones
    def crear_ubicacion(self, u: Ubicacion) -> int:
        try:
            with self._cx.transaccion() as cur:
                cur.execute(
                    """INSERT INTO ubicacion (codigo, nombre, tipo, pais, latitud, longitud)
                       VALUES (%s, %s, %s, %s, %s, %s) RETURNING id_ubicacion""",
                    (u.codigo, u.nombre, u.tipo, u.pais, u.latitud, u.longitud))
                u.id_ubicacion = cur.fetchone()["id_ubicacion"]
                _evento_ubicacion(cur, u)
                return u.id_ubicacion
        except psycopg.errors.UniqueViolation as exc:
            raise ErrorConflicto(f"Ya existe la ubicación {u.codigo}") from exc

    def asegurar_ubicacion(self, u: Ubicacion) -> bool:
        """Crea la ubicación si no existe (para los datos iniciales). Devuelve True si la creó."""
        with self._cx.transaccion() as cur:
            cur.execute(
                """INSERT INTO ubicacion (codigo, nombre, tipo, pais, latitud, longitud)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (codigo) DO NOTHING RETURNING id_ubicacion""",
                (u.codigo, u.nombre, u.tipo, u.pais, u.latitud, u.longitud))
            fila = cur.fetchone()
            if fila:
                u.id_ubicacion = fila["id_ubicacion"]
                _evento_ubicacion(cur, u)
            return fila is not None

    def ubicacion(self, codigo: str) -> Ubicacion | None:
        with self._cx.transaccion() as cur:
            cur.execute("SELECT * FROM ubicacion WHERE codigo = %s", (codigo,))
            f = cur.fetchone()
        return _a_ubicacion(f) if f else None

    def listar_ubicaciones(self) -> list[Ubicacion]:
        with self._cx.transaccion() as cur:
            cur.execute("SELECT * FROM ubicacion ORDER BY pais, codigo")
            return [_a_ubicacion(f) for f in cur.fetchall()]

    # ------------------------------------------------------------------ vehículos
    def crear_vehiculo(self, v: Vehiculo) -> int:
        try:
            with self._cx.transaccion() as cur:
                cur.execute(
                    """INSERT INTO vehiculo (tipo, identificacion, id_operador)
                       VALUES (%s, %s, %s) RETURNING id_vehiculo""",
                    (v.tipo, v.identificacion, v.id_operador))
                v.id_vehiculo = cur.fetchone()["id_vehiculo"]
                registrar_evento(cur, "vehiculo", v.id_vehiculo, "VEHICULO_GUARDADO",
                                 {"id_vehiculo": v.id_vehiculo, "tipo": v.tipo,
                                  "identificacion": v.identificacion, "id_operador": v.id_operador})
                return v.id_vehiculo
        except psycopg.errors.UniqueViolation as exc:
            raise ErrorConflicto(f"Ya existe un vehículo con la identificación {v.identificacion}") from exc

    def vehiculo(self, id_vehiculo: int) -> Vehiculo | None:
        lista = self.listar_vehiculos(id_vehiculo=id_vehiculo)
        return lista[0] if lista else None

    def listar_vehiculos(self, id_operador: int | None = None, id_vehiculo: int | None = None) -> list[Vehiculo]:
        sql = """SELECT v.*, a.razon_social AS operador FROM vehiculo v
                 JOIN actor a ON a.id_actor = v.id_operador"""
        params: list = []
        if id_operador is not None:
            sql += " WHERE v.id_operador = %s"
            params.append(id_operador)
        elif id_vehiculo is not None:
            sql += " WHERE v.id_vehiculo = %s"
            params.append(id_vehiculo)
        with self._cx.transaccion() as cur:
            cur.execute(sql + " ORDER BY a.razon_social, v.tipo, v.identificacion", params)
            return [Vehiculo(id_vehiculo=f["id_vehiculo"], tipo=f["tipo"], identificacion=f["identificacion"],
                             id_operador=f["id_operador"], estado=f["estado"], operador=f["operador"])
                    for f in cur.fetchall()]

    # ------------------------------------------------------------------ tarifas
    def asegurar_tarifa(self, t: Tarifa) -> None:
        with self._cx.transaccion() as cur:
            cur.execute(
                """INSERT INTO tarifa (medio_transporte, precio_base, precio_kg, vigente_desde, vigente_hasta)
                   VALUES (%s, %s, %s, %s, %s) ON CONFLICT (medio_transporte, vigente_desde) DO NOTHING""",
                (t.medio_transporte, t.precio_base, t.precio_kg, t.vigente_desde, t.vigente_hasta))

    def tarifa_vigente(self, medio: str, dia: date) -> Tarifa | None:
        """La tarifa del medio cuya vigencia incluye el día (la más reciente si hay varias)."""
        with self._cx.transaccion() as cur:
            cur.execute(
                """SELECT * FROM tarifa
                    WHERE medio_transporte = %s AND vigente_desde <= %s
                      AND (vigente_hasta IS NULL OR vigente_hasta > %s)
                    ORDER BY vigente_desde DESC LIMIT 1""", (medio, dia, dia))
            f = cur.fetchone()
        return _a_tarifa(f) if f else None

    def listar_tarifas(self) -> list[Tarifa]:
        with self._cx.transaccion() as cur:
            cur.execute("SELECT * FROM tarifa ORDER BY medio_transporte, vigente_desde DESC")
            return [_a_tarifa(f) for f in cur.fetchall()]


def _a_tarifa(f: dict) -> Tarifa:
    return Tarifa(id_tarifa=f["id_tarifa"], medio_transporte=f["medio_transporte"],
                  precio_base=float(f["precio_base"]), precio_kg=float(f["precio_kg"]),
                  vigente_desde=f["vigente_desde"], vigente_hasta=f["vigente_hasta"])
