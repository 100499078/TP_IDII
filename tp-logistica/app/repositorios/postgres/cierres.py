"""Cierre mensual de las cuentas corrientes (tabla cierre_mensual).

Una sola sentencia INSERT ... SELECT cierra todas las cuentas del período:
    saldo_inicial  = todos los movimientos ANTERIORES al mes
    total_debitos  = débitos del mes
    total_creditos = créditos del mes
    saldo_final    = inicial + débitos - créditos   (la tabla tiene un CHECK que lo exige)
ON CONFLICT (id_cuenta, periodo) DO NOTHING lo hace IDEMPOTENTE: correrlo dos veces
para el mismo mes no duplica ni cambia nada.
"""
from __future__ import annotations

from datetime import datetime

from app.infra.conexiones import GestorConexiones


class RepositorioCierres:
    def __init__(self, conexiones: GestorConexiones):
        self._cx = conexiones

    def cerrar_periodo(self, periodo: str, desde: datetime, hasta: datetime) -> int:
        """Cierra el período [desde, hasta) para todas las cuentas. Devuelve cuántas cerró ahora."""
        with self._cx.transaccion() as cur:
            cur.execute(
                """INSERT INTO cierre_mensual (id_cuenta, periodo, saldo_inicial, total_debitos, total_creditos,
                                               saldo_final)
                   SELECT cc.id_cuenta, %(periodo)s, x.inicial, x.debitos, x.creditos,
                          x.inicial + x.debitos - x.creditos
                     FROM cuenta_corriente cc
                    CROSS JOIN LATERAL (
                          SELECT coalesce(sum(CASE m.tipo WHEN 'DEBITO' THEN m.importe ELSE -m.importe END)
                                              FILTER (WHERE m.fecha < %(desde)s), 0) AS inicial,
                                 coalesce(sum(m.importe) FILTER (WHERE m.tipo = 'DEBITO'
                                              AND m.fecha >= %(desde)s AND m.fecha < %(hasta)s), 0) AS debitos,
                                 coalesce(sum(m.importe) FILTER (WHERE m.tipo = 'CREDITO'
                                              AND m.fecha >= %(desde)s AND m.fecha < %(hasta)s), 0) AS creditos
                            FROM movimiento_cc m WHERE m.id_cuenta = cc.id_cuenta) x
                   ON CONFLICT (id_cuenta, periodo) DO NOTHING""",
                {"periodo": periodo, "desde": desde, "hasta": hasta})
            return cur.rowcount

    def de_actor(self, id_actor: int, limite: int = 24) -> list[dict]:
        with self._cx.transaccion() as cur:
            cur.execute("""SELECT c.* FROM cierre_mensual c JOIN cuenta_corriente cc USING (id_cuenta)
                            WHERE cc.id_actor = %s ORDER BY c.periodo DESC LIMIT %s""", (id_actor, limite))
            return cur.fetchall()
