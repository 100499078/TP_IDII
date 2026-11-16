"""Consultas de control para el verificador de consistencia (fase 8).

Ninguna escribe: solo buscan datos que NO deberían existir (saldos que no coinciden con
sus movimientos, facturas cuyo estado no cuadra con sus pagos, etc.). Si todo está bien,
cada una devuelve una lista vacía.
"""
from __future__ import annotations

from app.infra.conexiones import GestorConexiones


class RepositorioControles:
    def __init__(self, conexiones: GestorConexiones):
        self._cx = conexiones

    def _consultar(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._cx.transaccion() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def estados_envios(self) -> dict[int, str]:
        """{id_envio: estado} de todos los envíos (la fuente de verdad, para comparar con Neo4j)."""
        return {f["id_envio"]: f["estado"] for f in self._consultar("SELECT id_envio, estado FROM envio")}

    def cuentas_descuadradas(self) -> list[dict]:
        """El saldo que mantiene el trigger tiene que ser igual a la suma de los movimientos."""
        return self._consultar(
            """SELECT a.razon_social, cc.saldo,
                      coalesce(sum(CASE m.tipo WHEN 'DEBITO' THEN m.importe ELSE -m.importe END), 0) AS calculado
                 FROM cuenta_corriente cc JOIN actor a USING (id_actor)
                 LEFT JOIN movimiento_cc m USING (id_cuenta)
                GROUP BY cc.id_cuenta, a.razon_social, cc.saldo
               HAVING cc.saldo <> coalesce(sum(CASE m.tipo WHEN 'DEBITO' THEN m.importe ELSE -m.importe END), 0)""")

    def facturas_descuadradas(self) -> list[dict]:
        """El estado de cada factura tiene que coincidir con lo que se le pagó."""
        return self._consultar(
            """SELECT f.numero, f.estado, f.importe_total, coalesce(p.pagado, 0) AS pagado
                 FROM factura f
                 LEFT JOIN (SELECT id_factura, sum(monto) AS pagado FROM pago GROUP BY id_factura) p USING (id_factura)
                WHERE coalesce(p.pagado, 0) > f.importe_total
                   OR (f.estado = 'PAGADA' AND coalesce(p.pagado, 0) <> f.importe_total)
                   OR (f.estado = 'PENDIENTE' AND coalesce(p.pagado, 0) > 0)
                   OR (f.estado = 'PARCIAL' AND (coalesce(p.pagado, 0) = 0
                                                 OR coalesce(p.pagado, 0) >= f.importe_total))""")

    def cierres_que_no_encadenan(self) -> list[dict]:
        """El saldo inicial de un mes tiene que ser el saldo final del mes anterior (si los dos están cerrados)."""
        return self._consultar(
            """SELECT a.razon_social, x.periodo, x.saldo_inicial, x.final_anterior
                 FROM (SELECT c.*, lag(c.saldo_final) OVER w AS final_anterior, lag(c.periodo) OVER w AS anterior
                         FROM cierre_mensual c
                       WINDOW w AS (PARTITION BY c.id_cuenta ORDER BY c.periodo)) x
                 JOIN cuenta_corriente cc USING (id_cuenta) JOIN actor a USING (id_actor)
                WHERE x.anterior = to_char(to_date(x.periodo, 'YYYY-MM') - interval '1 month', 'YYYY-MM')
                  AND x.saldo_inicial <> x.final_anterior""")

    def cierres_desactualizados(self) -> list[dict]:
        """Un mes cerrado no debería cambiar: su saldo final tiene que seguir siendo la suma de los
        movimientos hasta fin de ese mes (si no, alguien cargó un movimiento con fecha de un mes cerrado)."""
        return self._consultar(
            """SELECT a.razon_social, c.periodo, c.saldo_final, x.calculado
                 FROM cierre_mensual c
                 JOIN cuenta_corriente cc USING (id_cuenta) JOIN actor a USING (id_actor)
                 CROSS JOIN LATERAL (
                       SELECT coalesce(sum(CASE m.tipo WHEN 'DEBITO' THEN m.importe ELSE -m.importe END), 0) AS calculado
                         FROM movimiento_cc m
                        WHERE m.id_cuenta = c.id_cuenta
                          AND m.fecha < to_date(c.periodo, 'YYYY-MM') + interval '1 month') x
                WHERE c.saldo_final <> x.calculado""")

    def contenedores_inconsistentes(self) -> list[dict]:
        """Un contenedor EN_USO tiene que estar en exactamente un envío en curso, y uno DISPONIBLE en ninguno."""
        return self._consultar(
            """SELECT k.codigo_internacional, k.estado, count(e.id_envio) AS envios_en_curso
                 FROM contenedor k
                 LEFT JOIN envio_contenedor ec USING (id_contenedor)
                 LEFT JOIN envio e ON e.id_envio = ec.id_envio
                                  AND e.estado IN ('CREADO', 'EN_TRANSITO', 'EN_ADUANA', 'DEMORADO')
                GROUP BY k.id_contenedor, k.codigo_internacional, k.estado
               HAVING (k.estado = 'EN_USO' AND count(e.id_envio) <> 1)
                   OR (k.estado = 'DISPONIBLE' AND count(e.id_envio) > 0)""")

    def reportes_en_facturas(self) -> set[str]:
        """Fase 9: los reportes de MongoDB que PostgreSQL tiene cobrados en alguna factura."""
        return {f["id_reporte"] for f in self._consultar(
            "SELECT id_reporte FROM item_factura WHERE id_reporte IS NOT NULL")}
