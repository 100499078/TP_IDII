"""Envíos y tramos en PostgreSQL (fuente de verdad), con sus eventos para Neo4j."""
from __future__ import annotations

from datetime import datetime

from app.dominio.modelos import Envio, Tramo
from app.excepciones import ErrorConflicto
from app.infra.conexiones import GestorConexiones
from app.repositorios.postgres.outbox import registrar_evento

_SELECT_TRAMOS = """
SELECT t.*, o.codigo AS origen, d.codigo AS destino, a.razon_social AS operador, v.identificacion AS vehiculo
  FROM tramo t
  JOIN ubicacion o ON o.id_ubicacion = t.id_origen
  JOIN ubicacion d ON d.id_ubicacion = t.id_destino
  JOIN actor a     ON a.id_actor = t.id_operador
  LEFT JOIN vehiculo v ON v.id_vehiculo = t.id_vehiculo
"""


def _a_tramo(f: dict) -> Tramo:
    return Tramo(id_tramo=f["id_tramo"], id_envio=f["id_envio"], orden=f["orden"],
                 medio_transporte=f["medio_transporte"], origen=f["origen"], destino=f["destino"],
                 id_operador=f["id_operador"], id_vehiculo=f["id_vehiculo"], fecha_salida=f["fecha_salida"],
                 fecha_llegada_estimada=f["fecha_llegada_estimada"], fecha_llegada_real=f["fecha_llegada_real"],
                 costo=float(f["costo"]), operador=f["operador"], vehiculo=f["vehiculo"])


class RepositorioEnvios:
    def __init__(self, conexiones: GestorConexiones):
        self._cx = conexiones

    # ------------------------------------------------------------------ escritura
    def crear(self, e: Envio) -> int:
        """Una sola transacción: envío + contenedores asignados (pasan a EN_USO) + evento outbox."""
        with self._cx.transaccion() as cur:
            # FOR UPDATE bloquea los contenedores hasta el COMMIT: si dos operadores intentan
            # usar el mismo contenedor a la vez, el segundo espera y después ve que ya no está libre.
            cur.execute("""SELECT id_contenedor, estado FROM contenedor
                            WHERE id_contenedor = ANY(%s) FOR UPDATE""", (e.contenedores,))
            ocupados = [f["id_contenedor"] for f in cur.fetchall() if f["estado"] != "DISPONIBLE"]
            if ocupados:
                raise ErrorConflicto(f"Los contenedores {ocupados} ya no están disponibles")
            cur.execute(
                """INSERT INTO envio (id_cliente, id_origen, id_destino, fecha_entrega_estimada,
                                      prioridad, peso_kg, volumen_m3, fecha_creacion)
                   VALUES (%s, (SELECT id_ubicacion FROM ubicacion WHERE codigo = %s),
                               (SELECT id_ubicacion FROM ubicacion WHERE codigo = %s), %s, %s, %s, %s,
                           coalesce(%s, now()))
                   RETURNING id_envio, fecha_creacion, estado""",
                (e.id_cliente, e.origen, e.destino, e.fecha_entrega_estimada, e.prioridad, e.peso_kg, e.volumen_m3,
                 e.fecha_creacion))
            fila = cur.fetchone()
            e.id_envio, e.fecha_creacion, e.estado = fila["id_envio"], fila["fecha_creacion"], fila["estado"]
            for id_contenedor in e.contenedores:
                cur.execute("INSERT INTO envio_contenedor (id_envio, id_contenedor) VALUES (%s, %s)",
                            (e.id_envio, id_contenedor))
            cur.execute("UPDATE contenedor SET estado = 'EN_USO' WHERE id_contenedor = ANY(%s)", (e.contenedores,))
            registrar_evento(cur, "envio", e.id_envio, "ENVIO_CREADO", {
                "id_envio": e.id_envio, "id_cliente": e.id_cliente, "estado": e.estado, "prioridad": e.prioridad,
                "origen": e.origen, "destino": e.destino, "fecha_creacion": e.fecha_creacion,
                "contenedores": [{"id_contenedor": k, "desde": e.fecha_creacion} for k in e.contenedores]})
            return e.id_envio

    def agregar_tramo(self, t: Tramo) -> int:
        with self._cx.transaccion() as cur:
            # Bloquear la fila del envío serializa los tramos: dos tramos no pueden tomar el mismo orden
            cur.execute("SELECT id_envio FROM envio WHERE id_envio = %s FOR UPDATE", (t.id_envio,))
            cur.execute("SELECT coalesce(max(orden), 0) + 1 AS siguiente FROM tramo WHERE id_envio = %s",
                        (t.id_envio,))
            t.orden = cur.fetchone()["siguiente"]
            cur.execute(
                """INSERT INTO tramo (id_envio, orden, medio_transporte, id_origen, id_destino, id_operador,
                                      id_vehiculo, fecha_salida, fecha_llegada_estimada, costo)
                   VALUES (%s, %s, %s, (SELECT id_ubicacion FROM ubicacion WHERE codigo = %s),
                           (SELECT id_ubicacion FROM ubicacion WHERE codigo = %s), %s, %s, %s, %s, %s)
                   RETURNING id_tramo""",
                (t.id_envio, t.orden, t.medio_transporte, t.origen, t.destino, t.id_operador, t.id_vehiculo,
                 t.fecha_salida, t.fecha_llegada_estimada, t.costo))
            t.id_tramo = cur.fetchone()["id_tramo"]
            registrar_evento(cur, "tramo", t.id_tramo, "TRAMO_AGREGADO", {
                "id_tramo": t.id_tramo, "id_envio": t.id_envio, "orden": t.orden, "medio": t.medio_transporte,
                "codigo_origen": t.origen, "codigo_destino": t.destino, "id_operador": t.id_operador,
                "id_vehiculo": t.id_vehiculo, "costo": t.costo, "fecha_salida": t.fecha_salida,
                "fecha_llegada_estimada": t.fecha_llegada_estimada})
            return t.id_tramo

    def cambiar_estado(self, id_envio: int, estado: str, fecha: datetime, liberar_contenedores: bool) -> list[int]:
        """Cambia el estado; si el envío terminó, libera sus contenedores. Devuelve los liberados."""
        with self._cx.transaccion() as cur:
            cur.execute(
                """UPDATE envio SET estado = %s,
                          fecha_entrega_real = CASE WHEN %s = 'ENTREGADO' THEN %s ELSE fecha_entrega_real END
                    WHERE id_envio = %s""", (estado, estado, fecha, id_envio))
            liberados: list[int] = []
            if liberar_contenedores:
                cur.execute(
                    """UPDATE contenedor SET estado = 'DISPONIBLE'
                        WHERE id_contenedor IN (SELECT id_contenedor FROM envio_contenedor WHERE id_envio = %s)
                          AND estado = 'EN_USO' RETURNING id_contenedor""", (id_envio,))
                liberados = [f["id_contenedor"] for f in cur.fetchall()]
            registrar_evento(cur, "envio", id_envio, "ENVIO_ESTADO", {
                "id_envio": id_envio, "estado": estado, "fecha": fecha, "contenedores_liberados": liberados})
            return liberados

    # ------------------------------------------------------------------ fase 9
    def modificar(self, id_envio: int, prioridad: str, fecha_entrega_estimada: datetime, ahora: datetime) -> None:
        with self._cx.transaccion() as cur:
            cur.execute("UPDATE envio SET prioridad = %s, fecha_entrega_estimada = %s WHERE id_envio = %s",
                        (prioridad, fecha_entrega_estimada, id_envio))
            registrar_evento(cur, "envio", id_envio, "ENVIO_MODIFICADO", {
                "id_envio": id_envio, "prioridad": prioridad, "fecha_entrega_estimada": fecha_entrega_estimada,
                "fecha": ahora})

    def agregar_contenedor(self, id_envio: int, id_contenedor: int, ahora: datetime) -> None:
        """Contenedor + envío + evento en UNA transacción; FOR UPDATE evita que otro envío lo tome a la vez."""
        with self._cx.transaccion() as cur:
            cur.execute("SELECT estado FROM contenedor WHERE id_contenedor = %s FOR UPDATE", (id_contenedor,))
            fila = cur.fetchone()
            if fila is None or fila["estado"] != "DISPONIBLE":
                raise ErrorConflicto(f"El contenedor {id_contenedor} ya no está disponible")
            cur.execute("INSERT INTO envio_contenedor (id_envio, id_contenedor) VALUES (%s, %s)",
                        (id_envio, id_contenedor))
            cur.execute("UPDATE contenedor SET estado = 'EN_USO' WHERE id_contenedor = %s", (id_contenedor,))
            registrar_evento(cur, "envio", id_envio, "CONTENEDOR_ASIGNADO",
                             {"id_envio": id_envio, "id_contenedor": id_contenedor, "desde": ahora})

    def quitar_contenedor(self, id_envio: int, id_contenedor: int) -> None:
        with self._cx.transaccion() as cur:
            cur.execute("DELETE FROM envio_contenedor WHERE id_envio = %s AND id_contenedor = %s",
                        (id_envio, id_contenedor))
            cur.execute("UPDATE contenedor SET estado = 'DISPONIBLE' WHERE id_contenedor = %s AND estado = 'EN_USO'",
                        (id_contenedor,))
            registrar_evento(cur, "envio", id_envio, "CONTENEDOR_QUITADO",
                             {"id_envio": id_envio, "id_contenedor": id_contenedor})

    def registrar_llegada(self, t: Tramo, fecha: datetime) -> None:
        demora_h = round((fecha - t.fecha_llegada_estimada).total_seconds() / 3600, 2)
        with self._cx.transaccion() as cur:
            cur.execute("UPDATE tramo SET fecha_llegada_real = %s WHERE id_tramo = %s", (fecha, t.id_tramo))
            registrar_evento(cur, "tramo", t.id_tramo, "TRAMO_LLEGADA",
                             {"id_tramo": t.id_tramo, "fecha": fecha, "demora_h": demora_h})

    # ------------------------------------------------------------------ lectura
    def obtener(self, id_envio: int) -> Envio | None:
        with self._cx.transaccion() as cur:
            cur.execute(
                """SELECT e.*, o.codigo AS origen, d.codigo AS destino, a.razon_social AS cliente
                     FROM envio e
                     JOIN ubicacion o ON o.id_ubicacion = e.id_origen
                     JOIN ubicacion d ON d.id_ubicacion = e.id_destino
                     JOIN actor a ON a.id_actor = e.id_cliente
                    WHERE e.id_envio = %s""", (id_envio,))
            f = cur.fetchone()
            if f is None:
                return None
            cur.execute("SELECT id_contenedor FROM envio_contenedor WHERE id_envio = %s ORDER BY 1", (id_envio,))
            contenedores = [x["id_contenedor"] for x in cur.fetchall()]
            cur.execute(_SELECT_TRAMOS + " WHERE t.id_envio = %s ORDER BY t.orden", (id_envio,))
            tramos = [_a_tramo(x) for x in cur.fetchall()]
        return Envio(id_envio=f["id_envio"], id_cliente=f["id_cliente"], origen=f["origen"], destino=f["destino"],
                     fecha_entrega_estimada=f["fecha_entrega_estimada"], peso_kg=float(f["peso_kg"]),
                     volumen_m3=float(f["volumen_m3"]), prioridad=f["prioridad"], estado=f["estado"],
                     fecha_creacion=f["fecha_creacion"], fecha_entrega_real=f["fecha_entrega_real"],
                     cliente=f["cliente"], contenedores=contenedores, tramos=tramos)

    def tramo(self, id_tramo: int) -> Tramo | None:
        with self._cx.transaccion() as cur:
            cur.execute(_SELECT_TRAMOS + " WHERE t.id_tramo = %s", (id_tramo,))
            f = cur.fetchone()
        return _a_tramo(f) if f else None

    def listar(self, estado: str | None = None, id_cliente: int | None = None, limite: int = 100,
               id_operador: int | None = None) -> list[dict]:
        condiciones, params = [], []
        if id_operador is not None:                       # fase 9: los envíos en los que opera un tramo
            condiciones.append("EXISTS (SELECT 1 FROM tramo t WHERE t.id_envio = e.id_envio AND t.id_operador = %s)")
            params.append(id_operador)
        if estado:
            condiciones.append("e.estado = %s")
            params.append(estado)
        if id_cliente is not None:
            condiciones.append("e.id_cliente = %s")
            params.append(id_cliente)
        where = (" WHERE " + " AND ".join(condiciones)) if condiciones else ""
        with self._cx.transaccion() as cur:
            cur.execute(
                f"""SELECT e.id_envio, a.razon_social AS cliente, o.codigo AS origen, d.codigo AS destino,
                           e.estado, e.prioridad, e.fecha_entrega_estimada,
                           (SELECT count(*) FROM tramo t WHERE t.id_envio = e.id_envio) AS tramos
                      FROM envio e
                      JOIN actor a ON a.id_actor = e.id_cliente
                      JOIN ubicacion o ON o.id_ubicacion = e.id_origen
                      JOIN ubicacion d ON d.id_ubicacion = e.id_destino
                      {where} ORDER BY e.id_envio DESC LIMIT %s""", params + [limite])
            return cur.fetchall()

    # ------------------------------------------------------------------ para reconstruir el grafo
    def todos_para_grafo(self) -> tuple[list[dict], list[dict]]:
        """Envíos (con contenedores) y tramos en el orden en que hay que aplicarlos al grafo."""
        with self._cx.transaccion() as cur:
            cur.execute(
                """SELECT e.id_envio, e.id_cliente, e.estado, e.prioridad, e.fecha_creacion,
                          o.codigo AS origen, d.codigo AS destino,
                          coalesce(json_agg(json_build_object('id_contenedor', ec.id_contenedor,
                                                              'desde', e.fecha_creacion))
                                   FILTER (WHERE ec.id_contenedor IS NOT NULL), '[]') AS contenedores
                     FROM envio e
                     JOIN ubicacion o ON o.id_ubicacion = e.id_origen
                     JOIN ubicacion d ON d.id_ubicacion = e.id_destino
                     LEFT JOIN envio_contenedor ec ON ec.id_envio = e.id_envio
                    GROUP BY e.id_envio, o.codigo, d.codigo ORDER BY e.id_envio""")
            envios = cur.fetchall()
            cur.execute(_SELECT_TRAMOS + " ORDER BY t.id_envio, t.orden")
            tramos = cur.fetchall()
        return envios, tramos
