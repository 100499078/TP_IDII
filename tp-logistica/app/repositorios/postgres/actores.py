"""Clientes, operadores logísticos y proveedores (supertipo `actor` + subtipo)."""
from __future__ import annotations

import psycopg

from app.dominio.modelos import ESTADOS_ENVIO_EN_CURSO, Actor
from app.excepciones import ErrorConflicto
from app.infra.conexiones import GestorConexiones
from app.repositorios.postgres.outbox import registrar_evento

_SELECT = """
SELECT a.id_actor, a.tipo, a.razon_social, a.cuit, a.email, a.telefono, a.direccion, a.pais, a.estado,
       c.limite_credito, c.condicion_pago_dias, o.medios, p.rubro
  FROM actor a
  LEFT JOIN cliente c            ON c.id_actor = a.id_actor
  LEFT JOIN operador_logistico o ON o.id_actor = a.id_actor
  LEFT JOIN proveedor p          ON p.id_actor = a.id_actor
"""


def _a_actor(f: dict) -> Actor:
    return Actor(id_actor=f["id_actor"], tipo=f["tipo"], razon_social=f["razon_social"], cuit=f["cuit"],
                 email=f["email"], telefono=f["telefono"], direccion=f["direccion"], pais=f["pais"],
                 estado=f["estado"],
                 limite_credito=float(f["limite_credito"]) if f["limite_credito"] is not None else None,
                 condicion_pago_dias=f["condicion_pago_dias"], medios=list(f["medios"] or []), rubro=f["rubro"])


def _evento(cur, a: Actor) -> None:
    # Solo clientes y operadores existen en el grafo (los proveedores no participan de las rutas)
    if a.tipo in ("CLIENTE", "OPERADOR"):
        registrar_evento(cur, "actor", a.id_actor, "ACTOR_GUARDADO",
                         {"id_actor": a.id_actor, "tipo": a.tipo, "razon_social": a.razon_social,
                          "pais": a.pais, "estado": a.estado})


class RepositorioActores:
    def __init__(self, conexiones: GestorConexiones):
        self._cx = conexiones

    def crear(self, a: Actor) -> int:
        """Inserta actor + subtipo + cuenta corriente + evento outbox, en UNA transacción."""
        try:
            with self._cx.transaccion() as cur:
                cur.execute(
                    """INSERT INTO actor (tipo, razon_social, cuit, email, telefono, direccion, pais)
                       VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id_actor""",
                    (a.tipo, a.razon_social, a.cuit, a.email, a.telefono, a.direccion, a.pais))
                a.id_actor = cur.fetchone()["id_actor"]
                self._guardar_subtipo(cur, a, nuevo=True)
                cur.execute("INSERT INTO cuenta_corriente (id_actor) VALUES (%s)", (a.id_actor,))
                _evento(cur, a)
                return a.id_actor
        except psycopg.errors.UniqueViolation as exc:
            campo = "CUIT" if "cuit" in str(exc) else "email"
            raise ErrorConflicto(f"Ya existe un actor con ese {campo}") from exc

    def actualizar(self, a: Actor) -> None:
        try:
            with self._cx.transaccion() as cur:
                cur.execute(
                    """UPDATE actor SET razon_social = %s, email = %s, telefono = %s, direccion = %s,
                              pais = %s, estado = %s
                        WHERE id_actor = %s""",
                    (a.razon_social, a.email, a.telefono, a.direccion, a.pais, a.estado, a.id_actor))
                self._guardar_subtipo(cur, a, nuevo=False)
                _evento(cur, a)
        except psycopg.errors.UniqueViolation as exc:
            raise ErrorConflicto("Ya existe un actor con ese email") from exc

    @staticmethod
    def _guardar_subtipo(cur, a: Actor, nuevo: bool) -> None:
        if a.tipo == "CLIENTE":
            sql = ("INSERT INTO cliente (limite_credito, condicion_pago_dias, id_actor) VALUES (%s, %s, %s)" if nuevo
                   else "UPDATE cliente SET limite_credito = %s, condicion_pago_dias = %s WHERE id_actor = %s")
            cur.execute(sql, (a.limite_credito or 0, a.condicion_pago_dias or 30, a.id_actor))
        elif a.tipo == "OPERADOR":
            sql = ("INSERT INTO operador_logistico (medios, id_actor) VALUES (%s, %s)" if nuevo
                   else "UPDATE operador_logistico SET medios = %s WHERE id_actor = %s")
            cur.execute(sql, (a.medios, a.id_actor))
        else:
            sql = ("INSERT INTO proveedor (rubro, id_actor) VALUES (%s, %s)" if nuevo
                   else "UPDATE proveedor SET rubro = %s WHERE id_actor = %s")
            cur.execute(sql, (a.rubro, a.id_actor))

    def obtener(self, id_actor: int) -> Actor | None:
        with self._cx.transaccion() as cur:
            cur.execute(_SELECT + " WHERE a.id_actor = %s", (id_actor,))
            fila = cur.fetchone()
        return _a_actor(fila) if fila else None

    def listar(self, tipo: str | None = None, solo_activos: bool = False, texto: str | None = None) -> list[Actor]:
        condiciones, params = [], []
        if tipo:
            condiciones.append("a.tipo = %s")
            params.append(tipo)
        if solo_activos:
            condiciones.append("a.estado = 'ACTIVO'")
        if texto:
            condiciones.append("(a.razon_social ILIKE %s OR a.cuit LIKE %s)")
            params += [f"%{texto}%", f"%{texto}%"]
        where = (" WHERE " + " AND ".join(condiciones)) if condiciones else ""
        with self._cx.transaccion() as cur:
            cur.execute(_SELECT + where + " ORDER BY a.razon_social", params)
            return [_a_actor(f) for f in cur.fetchall()]

    def envios_en_curso_de_cliente(self, id_actor: int) -> int:
        with self._cx.transaccion() as cur:
            cur.execute("""SELECT count(*) AS n FROM envio WHERE id_cliente = %s
                           AND estado = ANY(%s)""", (id_actor, list(ESTADOS_ENVIO_EN_CURSO)))
            return cur.fetchone()["n"]

    def tramos_en_curso_de_operador(self, id_actor: int) -> int:
        with self._cx.transaccion() as cur:
            cur.execute("""SELECT count(*) AS n FROM tramo t JOIN envio e USING (id_envio)
                            WHERE t.id_operador = %s
                              AND e.estado = ANY(%s)""", (id_actor, list(ESTADOS_ENVIO_EN_CURSO)))
            return cur.fetchone()["n"]
