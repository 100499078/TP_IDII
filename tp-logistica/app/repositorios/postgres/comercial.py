"""Facturas, pagos y cuenta corriente en PostgreSQL.

Es el módulo donde más importa ACID: cada operación toca varias tablas (factura, ítems,
pagos, movimientos y, por el trigger, el saldo) y tiene que quedar todo o nada.

Convención del saldo de la cuenta corriente (lo mantiene el trigger fn_actualizar_saldo):
    DEBITO  suma  -> el actor nos debe más   (emitimos una factura, le pagamos a un operador)
    CREDITO resta -> el actor nos debe menos (nos paga, nos factura un operador, anulamos)
Saldo positivo = el actor nos debe; negativo = le debemos.
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import Decimal

import psycopg

from app.dominio.modelos import ESTADOS_FACTURA_ABIERTA, Factura, ItemFactura, PagoFactura, pesos
from app.excepciones import EntidadNoEncontrada, ErrorConflicto
from app.infra.conexiones import GestorConexiones


def _a_factura(f: dict) -> Factura:
    return Factura(id_actor=f["id_actor"], tipo=f["tipo"], numero=f["numero"], fecha_emision=f["fecha_emision"],
                   fecha_vencimiento=f["fecha_vencimiento"], importe_total=f["importe_total"], estado=f["estado"],
                   id_factura=f["id_factura"], razon_social=f.get("razon_social"))


def momento_del_asiento(fecha: date | datetime | None) -> datetime | None:
    """El asiento lleva la fecha del comprobante (fase 8). Un pago trae fecha y hora; una factura,
    solo el día: si es de hoy, se usa la hora actual (None -> now() en la base); si es de otro
    día (por ejemplo, en la carga de datos históricos), el mediodía de ese día."""
    if fecha is None or isinstance(fecha, datetime):
        return fecha
    if fecha == datetime.now(timezone.utc).date():
        return None
    return datetime.combine(fecha, time(12), tzinfo=timezone.utc)


class RepositorioComercial:
    def __init__(self, conexiones: GestorConexiones):
        self._cx = conexiones

    @staticmethod
    def _movimiento(cur, id_actor: int, tipo: str, importe: Decimal, descripcion: str,
                    id_factura: int | None = None, id_pago: int | None = None,
                    fecha: date | datetime | None = None) -> None:
        """Asiento en la cuenta corriente; el trigger actualiza el saldo en la misma transacción."""
        cur.execute(
            """INSERT INTO movimiento_cc (id_cuenta, tipo, importe, id_factura, id_pago, descripcion, fecha)
               SELECT id_cuenta, %s, %s, %s, %s, %s, coalesce(%s, now())
                 FROM cuenta_corriente WHERE id_actor = %s""",
            (tipo, importe, id_factura, id_pago, descripcion[:200], momento_del_asiento(fecha), id_actor))
        if cur.rowcount != 1:
            raise EntidadNoEncontrada(f"El actor {id_actor} no tiene cuenta corriente")

    # ------------------------------------------------------------------ qué se puede facturar
    def envios_facturables(self, id_cliente: int) -> list[dict]:
        """Envíos ENTREGADOS del cliente que todavía no están en ninguna factura, con el costo de su ruta."""
        with self._cx.transaccion() as cur:
            cur.execute(
                """SELECT e.id_envio, o.codigo AS origen, d.codigo AS destino, e.fecha_entrega_real,
                          coalesce(sum(t.costo), 0) AS costo
                     FROM envio e
                     JOIN ubicacion o ON o.id_ubicacion = e.id_origen
                     JOIN ubicacion d ON d.id_ubicacion = e.id_destino
                     LEFT JOIN tramo t ON t.id_envio = e.id_envio
                    WHERE e.id_cliente = %s AND e.estado = 'ENTREGADO'
                      AND NOT EXISTS (SELECT 1 FROM item_factura i WHERE i.id_envio = e.id_envio)
                    GROUP BY e.id_envio, o.codigo, d.codigo ORDER BY e.id_envio""", (id_cliente,))
            return cur.fetchall()

    def reportes_ya_facturados(self, ids: list[str]) -> dict[str, dict]:
        """{id_reporte: {id_factura, numero}} de los que ya están en una factura (aunque MongoDB no lo sepa)."""
        if not ids:
            return {}
        with self._cx.transaccion() as cur:
            cur.execute("""SELECT i.id_reporte, f.id_factura, f.numero FROM item_factura i JOIN factura f USING (id_factura)
                            WHERE i.id_reporte = ANY(%s)""", (list(ids),))
            return {r["id_reporte"]: r for r in cur.fetchall()}

    def clientes_con_envios_facturables(self) -> list[int]:
        with self._cx.transaccion() as cur:
            cur.execute("""SELECT DISTINCT e.id_cliente FROM envio e
                            WHERE e.estado = 'ENTREGADO'
                              AND NOT EXISTS (SELECT 1 FROM item_factura i WHERE i.id_envio = e.id_envio)
                            ORDER BY 1""")
            return [f["id_cliente"] for f in cur.fetchall()]

    # ------------------------------------------------------------------ emitir y recibir
    def emitir(self, id_cliente: int, items: list[ItemFactura], fecha: date, vencimiento: date) -> Factura:
        """Factura EMITIDA + ítems + débito en la cuenta, en UNA transacción.
        El índice único ux_item_factura_envio impide facturar dos veces el mismo envío,
        aunque dos personas facturen al mismo cliente a la vez."""
        total = pesos(sum((i.subtotal for i in items), Decimal("0")))
        try:
            with self._cx.transaccion() as cur:
                # El número sale del mismo id: se pide el siguiente valor de la secuencia antes del INSERT
                cur.execute("SELECT nextval(pg_get_serial_sequence('factura', 'id_factura')) AS id")
                id_factura = cur.fetchone()["id"]
                numero = f"A-{id_factura:08d}"
                cur.execute(
                    """INSERT INTO factura (id_factura, id_actor, tipo, numero, fecha_emision, fecha_vencimiento,
                                            importe_total)
                       VALUES (%s, %s, 'EMITIDA', %s, %s, %s, %s)""",
                    (id_factura, id_cliente, numero, fecha, vencimiento, total))
                for i in items:
                    cur.execute("""INSERT INTO item_factura (id_factura, id_envio, id_reporte, descripcion, cantidad,
                                                             precio_unitario)
                                   VALUES (%s, %s, %s, %s, %s, %s)""",
                                (id_factura, i.id_envio, i.id_reporte, i.descripcion[:200], i.cantidad,
                                 i.precio_unitario))
                self._movimiento(cur, id_cliente, "DEBITO", total, f"Factura {numero}", id_factura=id_factura,
                                 fecha=fecha)
        except psycopg.errors.UniqueViolation as exc:
            raise ErrorConflicto("Alguno de los envíos o reportes ya fue facturado (¿otra persona facturó a la vez?)") from exc
        return Factura(id_cliente, "EMITIDA", numero, fecha, vencimiento, total, items=items, id_factura=id_factura)

    def registrar_recibida(self, id_actor: int, numero: str, fecha: date, vencimiento: date, importe: Decimal,
                           descripcion: str) -> int:
        """Factura que nos hace un operador o proveedor: CRÉDITO en su cuenta (le debemos)."""
        try:
            with self._cx.transaccion() as cur:
                cur.execute(
                    """INSERT INTO factura (id_actor, tipo, numero, fecha_emision, fecha_vencimiento, importe_total)
                       VALUES (%s, 'RECIBIDA', %s, %s, %s, %s) RETURNING id_factura""",
                    (id_actor, numero, fecha, vencimiento, importe))
                id_factura = cur.fetchone()["id_factura"]
                cur.execute("""INSERT INTO item_factura (id_factura, descripcion, cantidad, precio_unitario)
                               VALUES (%s, %s, 1, %s)""", (id_factura, descripcion[:200], importe))
                self._movimiento(cur, id_actor, "CREDITO", importe, f"Factura recibida {numero}", id_factura=id_factura,
                                 fecha=fecha)
                return id_factura
        except psycopg.errors.UniqueViolation as exc:
            raise ErrorConflicto(f"Ya está cargada una factura recibida con el número {numero}") from exc

    # ------------------------------------------------------------------ pagos y anulación
    def registrar_pago(self, id_factura: int, monto: Decimal, medio: str, fecha: datetime) -> str:
        """Pago + movimiento + nuevo estado de la factura, en UNA transacción.
        FOR UPDATE bloquea la factura: si dos pagos llegan a la vez, el segundo espera y ve
        el primero, así nunca se paga de más. Devuelve el estado nuevo."""
        with self._cx.transaccion() as cur:
            cur.execute("SELECT * FROM factura WHERE id_factura = %s FOR UPDATE", (id_factura,))
            f = cur.fetchone()
            if f is None:
                raise EntidadNoEncontrada(f"No existe la factura {id_factura}")
            if f["estado"] not in ESTADOS_FACTURA_ABIERTA:
                raise ErrorConflicto(f"La factura está {f['estado']}: no admite pagos")
            cur.execute("SELECT coalesce(sum(monto), 0) AS pagado FROM pago WHERE id_factura = %s", (id_factura,))
            pendiente = pesos(f["importe_total"] - cur.fetchone()["pagado"])
            if monto > pendiente:
                raise ErrorConflicto(f"El pago supera lo que falta pagar ($ {pendiente})")
            cur.execute("INSERT INTO pago (id_factura, fecha, monto, medio_pago) VALUES (%s, %s, %s, %s) "
                        "RETURNING id_pago", (id_factura, fecha, monto, medio))
            id_pago = cur.fetchone()["id_pago"]
            # Si nos pagan (EMITIDA) baja lo que nos deben; si pagamos (RECIBIDA) baja lo que les debemos
            tipo = "CREDITO" if f["tipo"] == "EMITIDA" else "DEBITO"
            self._movimiento(cur, f["id_actor"], tipo, monto, f"Pago de la factura {f['numero']} ({medio.lower()})",
                             id_pago=id_pago, fecha=fecha)
            nuevo = "PAGADA" if monto == pendiente else "PARCIAL"
            cur.execute("UPDATE factura SET estado = %s WHERE id_factura = %s", (nuevo, id_factura))
            return nuevo

    def anular(self, id_factura: int) -> list[str]:
        """Anula una factura sin pagos: contraasiento en la cuenta y los envíos (y reportes) quedan libres
        para refacturar. Devuelve los IDs de los reportes liberados (para desmarcarlos en MongoDB)."""
        with self._cx.transaccion() as cur:
            cur.execute("SELECT * FROM factura WHERE id_factura = %s FOR UPDATE", (id_factura,))
            f = cur.fetchone()
            if f is None:
                raise EntidadNoEncontrada(f"No existe la factura {id_factura}")
            if f["estado"] == "ANULADA":
                raise ErrorConflicto("La factura ya está anulada")
            cur.execute("SELECT count(*) AS n FROM pago WHERE id_factura = %s", (id_factura,))
            if cur.fetchone()["n"]:
                raise ErrorConflicto("La factura tiene pagos: no se puede anular")
            cur.execute("UPDATE factura SET estado = 'ANULADA' WHERE id_factura = %s", (id_factura,))
            # Se conserva qué envío era en la descripción y se libera el envío (índice único)
            cur.execute("SELECT id_reporte FROM item_factura WHERE id_factura = %s AND id_reporte IS NOT NULL",
                        (id_factura,))
            reportes = [f["id_reporte"] for f in cur.fetchall()]
            cur.execute("""UPDATE item_factura SET descripcion = left(descripcion || ' (anulado)', 200),
                                  id_envio = NULL, id_reporte = NULL
                            WHERE id_factura = %s""", (id_factura,))
            contrario = "CREDITO" if f["tipo"] == "EMITIDA" else "DEBITO"
            self._movimiento(cur, f["id_actor"], contrario, f["importe_total"], f"Anulación de la factura {f['numero']}",
                             id_factura=id_factura)
            return reportes

    def marcar_vencidas(self, hoy: date) -> int:
        with self._cx.transaccion() as cur:
            cur.execute("""UPDATE factura SET estado = 'VENCIDA'
                            WHERE estado IN ('PENDIENTE', 'PARCIAL') AND fecha_vencimiento < %s""", (hoy,))
            return cur.rowcount

    # ------------------------------------------------------------------ consultas
    def facturas(self, id_actor: int | None = None, solo_abiertas: bool = False, limite: int = 100) -> list[Factura]:
        condiciones, params = [], []
        if id_actor is not None:
            condiciones.append("f.id_actor = %s")
            params.append(id_actor)
        if solo_abiertas:
            condiciones.append("f.estado = ANY(%s)")
            params.append(list(ESTADOS_FACTURA_ABIERTA))
        where = (" WHERE " + " AND ".join(condiciones)) if condiciones else ""
        with self._cx.transaccion() as cur:
            cur.execute(f"""SELECT f.*, a.razon_social FROM factura f JOIN actor a USING (id_actor)
                            {where} ORDER BY f.fecha_emision DESC, f.id_factura DESC LIMIT %s""", params + [limite])
            facturas = [_a_factura(f) for f in cur.fetchall()]
            for fa in facturas:
                cur.execute("SELECT id_pago, fecha, monto, medio_pago FROM pago WHERE id_factura = %s ORDER BY fecha",
                            (fa.id_factura,))
                fa.pagos = [PagoFactura(**p) for p in cur.fetchall()]
        return facturas

    def factura(self, id_factura: int) -> Factura | None:
        with self._cx.transaccion() as cur:
            cur.execute("SELECT f.*, a.razon_social FROM factura f JOIN actor a USING (id_actor) WHERE id_factura = %s",
                        (id_factura,))
            fila = cur.fetchone()
            if fila is None:
                return None
            fa = _a_factura(fila)
            cur.execute("""SELECT id_item, id_envio, id_reporte, descripcion, cantidad, precio_unitario FROM item_factura
                            WHERE id_factura = %s ORDER BY id_item""", (id_factura,))
            fa.items = [ItemFactura(f["descripcion"], f["cantidad"], f["precio_unitario"], f["id_envio"], f["id_item"],
                                    f["id_reporte"]) for f in cur.fetchall()]
            cur.execute("SELECT id_pago, fecha, monto, medio_pago FROM pago WHERE id_factura = %s ORDER BY fecha",
                        (id_factura,))
            fa.pagos = [PagoFactura(**p) for p in cur.fetchall()]
            return fa

    def cuenta(self, id_actor: int) -> dict | None:
        with self._cx.transaccion() as cur:
            cur.execute("""SELECT cc.saldo, cc.fecha_actualizacion, c.limite_credito
                             FROM cuenta_corriente cc LEFT JOIN cliente c USING (id_actor)
                            WHERE cc.id_actor = %s""", (id_actor,))
            return cur.fetchone()

    def movimientos(self, id_actor: int, limite: int = 50) -> list[dict]:
        """Movimientos con el SALDO ACUMULADO de cada momento (función de ventana SUM() OVER)."""
        with self._cx.transaccion() as cur:
            cur.execute(
                """SELECT * FROM (
                       SELECT m.fecha, m.tipo, m.importe, m.descripcion,
                              sum(CASE m.tipo WHEN 'DEBITO' THEN m.importe ELSE -m.importe END) 
                                  OVER (ORDER BY m.fecha, m.id_movimiento) AS saldo
                         FROM movimiento_cc m JOIN cuenta_corriente cc USING (id_cuenta)
                        WHERE cc.id_actor = %s
                   ) x ORDER BY fecha DESC LIMIT %s""", (id_actor, limite))
            return list(reversed(cur.fetchall()))

    def antiguedad_deuda(self, hoy: date) -> list[dict]:
        """Lo que cada cliente debe, separado por antigüedad del vencimiento (informe de "aging")."""
        with self._cx.transaccion() as cur:
            cur.execute(
                """WITH pendientes AS (
                       SELECT f.id_actor, f.fecha_vencimiento,
                              f.importe_total - coalesce((SELECT sum(p.monto) FROM pago p
                                                          WHERE p.id_factura = f.id_factura), 0) AS pendiente
                         FROM factura f
                        WHERE f.tipo = 'EMITIDA' AND f.estado = ANY(%s))
                   SELECT a.id_actor, a.razon_social,
                          sum(pendiente) FILTER (WHERE fecha_vencimiento >= %s)                  AS a_vencer,
                          sum(pendiente) FILTER (WHERE %s - fecha_vencimiento BETWEEN 1 AND 30)  AS d1_30,
                          sum(pendiente) FILTER (WHERE %s - fecha_vencimiento BETWEEN 31 AND 60) AS d31_60,
                          sum(pendiente) FILTER (WHERE %s - fecha_vencimiento > 60)              AS mas_60,
                          sum(pendiente) AS total
                     FROM pendientes JOIN actor a USING (id_actor)
                    GROUP BY a.id_actor, a.razon_social
                   HAVING sum(pendiente) > 0
                    ORDER BY total DESC""",
                (list(ESTADOS_FACTURA_ABIERTA), hoy, hoy, hoy, hoy))
            return cur.fetchall()
