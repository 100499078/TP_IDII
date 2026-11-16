"""Consultas de los reportes que salen de PostgreSQL (envíos, tiempos, costos, clientes).

Cada método devuelve una lista de filas (dict). Los parámetros `desde` y `hasta` filtran
por la fecha de creación del envío (o de emisión de la factura).
"""
from __future__ import annotations

from datetime import date

from app.infra.conexiones import GestorConexiones


def _rango(columna: str, desde: date | None, hasta: date | None, id_cliente: int | None = None,
           columna_cliente: str = "e.id_cliente") -> tuple[str, list]:
    """El WHERE de los parámetros del reporte. Fase 9: id_cliente deja solo los datos de ese
    cliente (un reporte pedido por un cliente, o para cobrárselo, no puede mostrar a los demás)."""
    condiciones, params = [], []
    if id_cliente is not None:
        condiciones.append(f"{columna_cliente} = %s")
        params.append(id_cliente)
    if desde:
        condiciones.append(f"{columna} >= %s")
        params.append(desde)
    if hasta:
        condiciones.append(f"{columna} < %s::date + 1")      # hasta inclusive
        params.append(hasta)
    return (" AND ".join(condiciones) or "TRUE"), params


class RepositorioReportesSQL:
    def __init__(self, conexiones: GestorConexiones):
        self._cx = conexiones

    def _consultar(self, sql: str, params: list) -> list[dict]:
        with self._cx.transaccion() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def envios_por_estado(self, desde=None, hasta=None, id_cliente=None):
        filtro, p = _rango("e.fecha_creacion", desde, hasta, id_cliente)
        return self._consultar(f"""SELECT e.estado, count(*) AS envios FROM envio e WHERE {filtro}
                                   GROUP BY e.estado ORDER BY envios DESC""", p)

    def envios_por_pais(self, desde=None, hasta=None, id_cliente=None):
        filtro, p = _rango("e.fecha_creacion", desde, hasta, id_cliente)
        return self._consultar(
            f"""SELECT o.pais AS pais_origen, d.pais AS pais_destino, count(*) AS envios,
                       count(*) FILTER (WHERE e.estado = 'ENTREGADO') AS entregados
                  FROM envio e JOIN ubicacion o ON o.id_ubicacion = e.id_origen
                  JOIN ubicacion d ON d.id_ubicacion = e.id_destino
                 WHERE {filtro} GROUP BY o.pais, d.pais ORDER BY envios DESC""", p)

    def envios_por_cliente(self, desde=None, hasta=None, id_cliente=None):
        filtro, p = _rango("e.fecha_creacion", desde, hasta, id_cliente)
        return self._consultar(
            f"""SELECT a.razon_social AS cliente, count(*) AS envios,
                       count(*) FILTER (WHERE e.estado = 'ENTREGADO') AS entregados,
                       round(sum(e.peso_kg), 2) AS kg
                  FROM envio e JOIN actor a ON a.id_actor = e.id_cliente
                 WHERE {filtro} GROUP BY a.razon_social ORDER BY envios DESC""", p)

    def envios_demorados(self, desde=None, hasta=None, id_cliente=None):
        filtro, p = _rango("e.fecha_creacion", desde, hasta, id_cliente)
        return self._consultar(
            f"""SELECT e.id_envio, a.razon_social AS cliente, o.codigo || ' → ' || d.codigo AS ruta, e.estado,
                       e.fecha_entrega_estimada,
                       round(extract(epoch FROM (coalesce(e.fecha_entrega_real, now()) - e.fecha_entrega_estimada))
                             / 3600, 1) AS horas_de_atraso
                  FROM envio e JOIN actor a ON a.id_actor = e.id_cliente
                  JOIN ubicacion o ON o.id_ubicacion = e.id_origen
                  JOIN ubicacion d ON d.id_ubicacion = e.id_destino
                 WHERE {filtro} AND e.estado <> 'CANCELADO'
                   AND coalesce(e.fecha_entrega_real, now()) > e.fecha_entrega_estimada
                 ORDER BY horas_de_atraso DESC""", p)

    def tiempos_entrega(self, desde=None, hasta=None, id_cliente=None):
        """Por ruta: cuánto tardaron los envíos entregados y cuánto se desviaron de lo estimado."""
        filtro, p = _rango("e.fecha_creacion", desde, hasta, id_cliente)
        return self._consultar(
            f"""SELECT o.codigo || ' → ' || d.codigo AS ruta, count(*) AS entregados,
                       round(avg(extract(epoch FROM (e.fecha_entrega_real - e.fecha_creacion)) / 3600), 1)
                           AS horas_promedio,
                       round(avg(extract(epoch FROM (e.fecha_entrega_real - e.fecha_entrega_estimada)) / 3600), 1)
                           AS desvio_promedio_h,
                       count(*) FILTER (WHERE e.fecha_entrega_real <= e.fecha_entrega_estimada) AS a_tiempo
                  FROM envio e JOIN ubicacion o ON o.id_ubicacion = e.id_origen
                  JOIN ubicacion d ON d.id_ubicacion = e.id_destino
                 WHERE {filtro} AND e.estado = 'ENTREGADO'
                 GROUP BY o.codigo, d.codigo ORDER BY entregados DESC""", p)

    def costos_transporte(self, desde=None, hasta=None, id_cliente=None):
        filtro, p = _rango("t.fecha_salida", desde, hasta, id_cliente)
        return self._consultar(
            f"""SELECT t.medio_transporte AS medio, count(*) AS tramos, sum(t.costo) AS costo_total,
                       round(avg(t.costo), 2) AS costo_promedio,
                       round(sum(t.costo) / nullif(sum(e.peso_kg), 0), 4) AS costo_por_kg
                  FROM tramo t JOIN envio e USING (id_envio)
                 WHERE {filtro} GROUP BY t.medio_transporte ORDER BY costo_total DESC""", p)

    def ranking_clientes(self, desde=None, hasta=None, id_cliente=None):
        """Por VOLUMEN transportado (kg, envíos y contenedores); lo facturado va como dato aparte.
        Fase 8: antes ordenaba por facturación; el enunciado pide "por volumen transportado"."""
        filtro, p = _rango("e.fecha_creacion", desde, hasta, id_cliente)
        filtro_f, p_f = _rango("f.fecha_emision", desde, hasta, id_cliente, "f.id_actor")
        return self._consultar(
            f"""SELECT a.razon_social AS cliente, count(*) AS envios, round(sum(e.peso_kg), 0) AS kg,
                       round(sum(e.volumen_m3), 1) AS m3,
                       coalesce(sum(k.n), 0)::int AS contenedores,
                       coalesce((SELECT sum(f.importe_total) FROM factura f
                                  WHERE f.id_actor = a.id_actor AND f.tipo = 'EMITIDA' AND f.estado <> 'ANULADA'
                                    AND {filtro_f}), 0) AS facturado,
                       rank() OVER (ORDER BY sum(e.peso_kg) DESC) AS puesto
                  FROM envio e JOIN actor a ON a.id_actor = e.id_cliente
                  LEFT JOIN (SELECT id_envio, count(*) AS n FROM envio_contenedor GROUP BY id_envio) k
                         USING (id_envio)
                 WHERE {filtro} AND e.estado <> 'CANCELADO'
                 GROUP BY a.id_actor, a.razon_social ORDER BY puesto""", p_f + p)

    def tiempos_por_cliente(self, desde=None, hasta=None, id_cliente=None):
        """Tiempos mínimo, máximo y promedio de entrega por cliente, y qué porcentaje llegó a tiempo."""
        filtro, p = _rango("e.fecha_creacion", desde, hasta, id_cliente)
        return self._consultar(
            f"""SELECT a.razon_social AS cliente, count(*) AS entregados,
                       round(min(extract(epoch FROM (e.fecha_entrega_real - e.fecha_creacion)) / 3600), 1)
                           AS horas_minimo,
                       round(max(extract(epoch FROM (e.fecha_entrega_real - e.fecha_creacion)) / 3600), 1)
                           AS horas_maximo,
                       round(avg(extract(epoch FROM (e.fecha_entrega_real - e.fecha_creacion)) / 3600), 1)
                           AS horas_promedio,
                       round(100.0 * count(*) FILTER (WHERE e.fecha_entrega_real <= e.fecha_entrega_estimada)
                             / count(*), 1) AS porcentaje_a_tiempo
                  FROM envio e JOIN actor a ON a.id_actor = e.id_cliente
                 WHERE {filtro} AND e.estado = 'ENTREGADO'
                 GROUP BY a.razon_social ORDER BY porcentaje_a_tiempo, horas_promedio DESC""", p)

    def desempeno_mensual(self, desde=None, hasta=None, id_cliente=None):
        """Mes a mes: envíos, entregas, puntualidad y tiempos, con la variación contra el mes
        anterior calculada con la función de ventana lag()."""
        filtro, p = _rango("e.fecha_creacion", desde, hasta, id_cliente)
        return self._consultar(
            f"""WITH m AS (
                  SELECT date_trunc('month', e.fecha_creacion) AS mes, count(*) AS envios,
                         count(*) FILTER (WHERE e.estado = 'ENTREGADO') AS entregados,
                         count(*) FILTER (WHERE e.estado = 'CANCELADO') AS cancelados,
                         count(*) FILTER (WHERE e.estado = 'ENTREGADO'
                                            AND e.fecha_entrega_real <= e.fecha_entrega_estimada) AS a_tiempo,
                         avg(extract(epoch FROM (e.fecha_entrega_real - e.fecha_creacion)) / 3600)
                             FILTER (WHERE e.estado = 'ENTREGADO') AS horas
                    FROM envio e WHERE {filtro} GROUP BY 1)
                SELECT to_char(mes, 'YYYY-MM') AS mes, envios, entregados, cancelados,
                       round(100.0 * a_tiempo / nullif(entregados, 0), 1) AS porcentaje_a_tiempo,
                       round(horas, 1) AS horas_promedio,
                       envios - lag(envios) OVER (ORDER BY mes) AS variacion_envios,
                       round(100.0 * a_tiempo / nullif(entregados, 0)
                             - lag(100.0 * a_tiempo / nullif(entregados, 0)) OVER (ORDER BY mes), 1)
                           AS variacion_puntualidad
                  FROM m ORDER BY mes""", p)

    def envios_para_incidentes(self, desde=None, hasta=None, id_cliente=None):
        """Los envíos (que ya viajaron) con las dimensiones por las que se promedian los incidentes."""
        filtro, p = _rango("e.fecha_creacion", desde, hasta, id_cliente)
        return self._consultar(
            f"""SELECT e.id_envio, e.estado, a.razon_social AS cliente, o.pais AS pais_origen, d.pais AS pais_destino
                  FROM envio e JOIN actor a ON a.id_actor = e.id_cliente
                  JOIN ubicacion o ON o.id_ubicacion = e.id_origen
                  JOIN ubicacion d ON d.id_ubicacion = e.id_destino
                 WHERE {filtro} AND e.estado NOT IN ('CREADO', 'CANCELADO')""", p)

    def envios_en_curso(self) -> list[dict]:
        """Para la predicción de riesgo: envíos que todavía viajan, con sus tramos pendientes."""
        return self._consultar(
            """SELECT e.id_envio, e.estado, e.fecha_entrega_estimada,
                      max(t.fecha_llegada_estimada) AS llegada_planificada,
                      coalesce(json_agg(json_build_object('origen', o.codigo, 'destino', d.codigo,
                                                          'medio', t.medio_transporte))
                               FILTER (WHERE t.id_tramo IS NOT NULL AND t.fecha_llegada_real IS NULL), '[]')
                          AS tramos_pendientes
                 FROM envio e
                 LEFT JOIN tramo t ON t.id_envio = e.id_envio
                 LEFT JOIN ubicacion o ON o.id_ubicacion = t.id_origen
                 LEFT JOIN ubicacion d ON d.id_ubicacion = t.id_destino
                WHERE e.estado IN ('EN_TRANSITO', 'EN_ADUANA', 'DEMORADO')
                GROUP BY e.id_envio ORDER BY e.id_envio""", [])
