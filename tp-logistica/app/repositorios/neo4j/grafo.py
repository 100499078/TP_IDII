"""Grafo de la red logística en Neo4j.

El grafo es una PROYECCIÓN: no se escribe desde los menús, lo alimenta el
proceso de sincronización con los eventos del outbox de PostgreSQL. Por eso
todas las escrituras usan MERGE (crear si no existe): si un evento se procesa
dos veces, el resultado es el mismo (idempotencia).
"""
from __future__ import annotations

from contextlib import contextmanager

from app.excepciones import ErrorConexion
from app.infra.conexiones import GestorConexiones

# Para cada tipo de evento, las sentencias Cypher que lo aplican (todas en una transacción).
SENTENCIAS: dict[str, list[str]] = {
    "ACTOR_GUARDADO_CLIENTE": ["""
        MERGE (c:Cliente {id_actor: $id_actor})
        SET c.razon_social = $razon_social, c.pais = $pais, c.estado = $estado
        WITH c
        OPTIONAL MATCH (c)-[viejo:EN_PAIS]->(:Pais)
        DELETE viejo
        WITH c
        MERGE (p:Pais {codigo: $pais})
        MERGE (c)-[:EN_PAIS]->(p)"""],
    "ACTOR_GUARDADO_OPERADOR": ["""
        MERGE (o:Operador {id_actor: $id_actor})
        SET o.razon_social = $razon_social, o.pais = $pais, o.estado = $estado"""],
    "UBICACION_GUARDADA": ["""
        MERGE (u:Ubicacion {codigo: $codigo})
        SET u.nombre = $nombre, u.tipo = $tipo,
            u.punto = point({latitude: $latitud, longitude: $longitud})
        WITH u
        OPTIONAL MATCH (u)-[viejo:EN_PAIS]->(:Pais)
        DELETE viejo
        WITH u
        MERGE (p:Pais {codigo: $pais})
        MERGE (u)-[:EN_PAIS]->(p)"""],
    "CONTENEDOR_GUARDADO": ["""
        MERGE (k:Contenedor {id_contenedor: $id_contenedor})
        SET k.codigo = $codigo, k.tipo = $tipo"""],
    "VEHICULO_GUARDADO": ["""
        MERGE (v:Vehiculo {id_vehiculo: $id_vehiculo})
        SET v.tipo = $tipo, v.identificacion = $identificacion
        MERGE (o:Operador {id_actor: $id_operador})
        MERGE (v)-[:PERTENECE_A]->(o)"""],
    "ENVIO_CREADO": ["""
        MERGE (e:Envio {id_envio: $id_envio})
        SET e.estado = $estado, e.prioridad = $prioridad, e.origen = $origen, e.destino = $destino,
            e.fecha_creacion = datetime($fecha_creacion)
        MERGE (c:Cliente {id_actor: $id_cliente})
        MERGE (c)-[:SOLICITO]->(e)
        WITH e
        UNWIND $contenedores AS k
        MERGE (n:Contenedor {id_contenedor: k.id_contenedor})
        MERGE (n)-[v:VIAJA_EN]->(e)
        SET v.desde = datetime(k.desde)"""],
    "ENVIO_ESTADO": ["""
        MERGE (e:Envio {id_envio: $id_envio})
        SET e.estado = $estado
        WITH e
        UNWIND $contenedores_liberados AS id_k
        MATCH (:Contenedor {id_contenedor: id_k})-[v:VIAJA_EN]->(e)
        SET v.hasta = datetime($fecha)"""],
    # Fase 9: modificar un envío y cambiar sus contenedores antes de que salga
    "ENVIO_MODIFICADO": ["""
        MERGE (e:Envio {id_envio: $id_envio})
        SET e.prioridad = $prioridad, e.fecha_entrega_estimada = datetime($fecha_entrega_estimada)"""],
    "CONTENEDOR_ASIGNADO": ["""
        MERGE (e:Envio {id_envio: $id_envio})
        MERGE (k:Contenedor {id_contenedor: $id_contenedor})
        MERGE (k)-[v:VIAJA_EN]->(e)
        SET v.desde = datetime($desde)"""],
    "CONTENEDOR_QUITADO": ["""
        MATCH (:Contenedor {id_contenedor: $id_contenedor})-[v:VIAJA_EN]->(:Envio {id_envio: $id_envio})
        DELETE v"""],
    "TRAMO_AGREGADO": ["""
        MERGE (e:Envio {id_envio: $id_envio})
        MERGE (o:Ubicacion {codigo: $codigo_origen})
        MERGE (d:Ubicacion {codigo: $codigo_destino})
        MERGE (op:Operador {id_actor: $id_operador})
        MERGE (t:Tramo {id_tramo: $id_tramo})
          ON CREATE SET t.nuevo = true
          ON MATCH SET t.nuevo = false
        SET t.orden = $orden, t.medio = $medio, t.costo = $costo,
            t.fecha_salida = datetime($fecha_salida),
            t.fecha_llegada_estimada = datetime($fecha_llegada_estimada)
        MERGE (e)-[r:TIENE_TRAMO]->(t)
        SET r.orden = $orden
        MERGE (t)-[:DESDE]->(o)
        MERGE (t)-[:HASTA]->(d)
        MERGE (t)-[:OPERADO_POR]->(op)
        MERGE (o)-[ru:RUTA {medio: $medio}]->(d)
          ON CREATE SET ru.envios = 0, ru.incidentes = 0, ru.llegadas = 0, ru.demora_prom_h = 0.0
        WITH e, t, ru, t.nuevo AS nuevo
        // Solo se cuenta el tramo la primera vez que llega: reprocesar el evento no suma de nuevo
        SET ru.envios = ru.envios + CASE WHEN nuevo THEN 1 ELSE 0 END
        REMOVE t.nuevo
        WITH e, t
        OPTIONAL MATCH (e)-[:TIENE_TRAMO]->(anterior:Tramo {orden: t.orden - 1})
        FOREACH (_ IN CASE WHEN anterior IS NULL THEN [] ELSE [1] END | MERGE (anterior)-[:SIGUIENTE]->(t))""",
                       """
        MATCH (t:Tramo {id_tramo: $id_tramo})
        WITH t WHERE $id_vehiculo IS NOT NULL
        MERGE (v:Vehiculo {id_vehiculo: $id_vehiculo})
        MERGE (t)-[:EN_VEHICULO]->(v)"""],
    "TRAMO_LLEGADA": ["""
        MATCH (t:Tramo {id_tramo: $id_tramo})-[:DESDE]->(o:Ubicacion),
              (t)-[:HASTA]->(d:Ubicacion),
              (o)-[ru:RUTA {medio: t.medio}]->(d)
        WITH t, ru, t.fecha_llegada_real IS NULL AS primera,
             coalesce(ru.llegadas, 0) AS n, coalesce(ru.demora_prom_h, 0.0) AS promedio
        SET t.fecha_llegada_real = datetime($fecha)
        // promedio móvil de la demora de la ruta; solo la primera vez que se registra la llegada
        FOREACH (_ IN CASE WHEN primera THEN [1] ELSE [] END |
          SET ru.llegadas = n + 1, ru.demora_prom_h = (promedio * n + $demora_h) / (n + 1))"""],
    # Fase 4: un incidente (viene de MongoDB). El contador de la ruta sube UNA sola vez por
    # incidente, aunque el evento llegue dos veces (misma técnica que en TRAMO_AGREGADO).
    "INCIDENTE_REGISTRADO": ["""
        MERGE (i:Incidente {id_incidente: $id_incidente})
          ON CREATE SET i.nuevo = true
          ON MATCH SET i.nuevo = false
        SET i.tipo = $tipo, i.severidad = $severidad, i.fecha = datetime($fecha)
        MERGE (e:Envio {id_envio: $id_envio})
        MERGE (e)-[:TUVO_INCIDENTE]->(i)
        WITH i, i.nuevo AS nuevo
        REMOVE i.nuevo
        WITH i, nuevo WHERE $id_tramo IS NOT NULL
        MATCH (t:Tramo {id_tramo: $id_tramo})-[:DESDE]->(o:Ubicacion),
              (t)-[:HASTA]->(d:Ubicacion),
              (o)-[ru:RUTA {medio: t.medio}]->(d)
        MERGE (i)-[:EN_TRAMO]->(t)
        SET ru.incidentes = ru.incidentes + CASE WHEN nuevo THEN 1 ELSE 0 END"""],
}

TIPOS_EVENTO = ("ACTOR_GUARDADO", "UBICACION_GUARDADA", "CONTENEDOR_GUARDADO", "VEHICULO_GUARDADO",
                "ENVIO_CREADO", "ENVIO_ESTADO", "TRAMO_AGREGADO", "TRAMO_LLEGADA", "INCIDENTE_REGISTRADO",
                "ENVIO_MODIFICADO", "CONTENEDOR_ASIGNADO", "CONTENEDOR_QUITADO")

RUTA_ENVIO = """
MATCH (e:Envio {id_envio: $id_envio})-[:TIENE_TRAMO]->(t:Tramo),
      (t)-[:DESDE]->(o:Ubicacion), (t)-[:HASTA]->(d:Ubicacion),
      (t)-[:OPERADO_POR]->(op:Operador)
OPTIONAL MATCH (t)-[:EN_VEHICULO]->(v:Vehiculo)
RETURN t.orden AS orden, o.codigo AS desde, d.codigo AS hasta, t.medio AS medio,
       op.razon_social AS operador, v.identificacion AS vehiculo
ORDER BY orden"""

# Fase 4 — rutas con más incidentes (tasa = incidentes / tramos que usaron la ruta)
RUTAS_PROBLEMATICAS = """
MATCH (a:Ubicacion)-[r:RUTA]->(b:Ubicacion)
WHERE r.envios > 0
RETURN a.codigo AS desde, b.codigo AS hasta, r.medio AS medio, r.incidentes AS incidentes,
       r.envios AS envios, round(toFloat(r.incidentes) / r.envios, 2) AS tasa,
       round(r.demora_prom_h, 1) AS demora_h
ORDER BY incidentes DESC, tasa DESC
LIMIT $limite"""

# Fase 4 — camino más corto entre dos ubicaciones usando solo rutas con tasa de incidentes baja
RUTA_ALTERNATIVA = """
MATCH (a:Ubicacion {codigo: $desde}), (b:Ubicacion {codigo: $hasta})
MATCH p = shortestPath((a)-[:RUTA*..6]->(b))
WHERE all(r IN relationships(p) WHERE r.envios > 0 AND toFloat(r.incidentes) / r.envios < $tasa_maxima)
RETURN [u IN nodes(p) | u.codigo] AS escalas, [r IN relationships(p) | r.medio] AS medios,
       length(p) AS tramos"""

# Fase 7 — estadísticas de varias rutas a la vez (para la predicción de riesgo)
ESTADISTICAS_RUTAS = """
UNWIND $rutas AS r
MATCH (:Ubicacion {codigo: r.origen})-[ru:RUTA {medio: r.medio}]->(:Ubicacion {codigo: r.destino})
RETURN r.origen AS origen, r.destino AS destino, r.medio AS medio, ru.envios AS envios,
       ru.incidentes AS incidentes, ru.demora_prom_h AS demora_h"""

# Fase 9: reconstrucción del recorrido de un contenedor (todos los envíos en que viajó)
HISTORIAL_CONTENEDOR = """
MATCH (k:Contenedor {id_contenedor: $id_contenedor})-[v:VIAJA_EN]->(e:Envio)
OPTIONAL MATCH (c:Cliente)-[:SOLICITO]->(e)
OPTIONAL MATCH (e)-[:TIENE_TRAMO]->(t:Tramo)-[:HASTA]->(d:Ubicacion)
WITH v, e, c, t, d ORDER BY t.orden
WITH v, e, c, collect(d.codigo) AS escalas
RETURN e.id_envio AS id_envio, e.estado AS estado, c.razon_social AS cliente, e.origen AS origen,
       escalas, v.desde AS desde, v.hasta AS hasta
ORDER BY desde"""

# Fase 9: red de relaciones de un cliente: qué operadores movieron sus envíos y con qué otros clientes los comparte
RED_CLIENTE = """
MATCH (c:Cliente {id_actor: $id_cliente})-[:SOLICITO]->(e:Envio)-[:TIENE_TRAMO]->(:Tramo)-[:OPERADO_POR]->(o:Operador)
WITH c, o, count(DISTINCT e) AS envios
OPTIONAL MATCH (o)<-[:OPERADO_POR]-(:Tramo)<-[:TIENE_TRAMO]-(:Envio)<-[:SOLICITO]-(otro:Cliente)
WHERE otro <> c
RETURN o.razon_social AS operador, envios, collect(DISTINCT otro.razon_social) AS otros_clientes
ORDER BY envios DESC"""

ESTADOS_ENVIOS = """
MATCH (e:Envio) RETURN e.id_envio AS id_envio, e.estado AS estado"""

CONTAR_INCIDENTES = """
MATCH (i:Incidente) RETURN count(i) AS n"""

RESUMEN = """
MATCH (n) WITH labels(n)[0] AS etiqueta, count(*) AS cantidad
RETURN etiqueta, cantidad ORDER BY etiqueta"""


@contextmanager
def _neo4j_disponible():
    """Traduce "Neo4j no responde" a ErrorConexion (un error conocido, con mensaje claro)."""
    from neo4j.exceptions import ServiceUnavailable, SessionExpired
    try:
        yield
    except (ServiceUnavailable, SessionExpired, OSError) as exc:
        raise ErrorConexion("Neo4j") from exc


class RepositorioGrafo:
    def __init__(self, conexiones: GestorConexiones):
        self._cx = conexiones

    def verificar(self) -> None:
        """Falla con ErrorConexion si Neo4j no responde (se usa antes de leer la cola)."""
        with _neo4j_disponible():
            self._cx.neo4j.verify_connectivity()

    @staticmethod
    def sentencias_de(tipo_evento: str, payload: dict) -> list[str]:
        if tipo_evento == "ACTOR_GUARDADO":
            clave = f"ACTOR_GUARDADO_{payload['tipo']}"
            return SENTENCIAS.get(clave, [])          # los proveedores no van al grafo
        if tipo_evento not in SENTENCIAS:
            raise ValueError(f"Tipo de evento desconocido: {tipo_evento}")
        return SENTENCIAS[tipo_evento]

    def aplicar_evento(self, tipo_evento: str, payload: dict, id_evento: str | None = None) -> None:
        """Aplica un evento en UNA transacción de escritura de Neo4j."""
        sentencias = self.sentencias_de(tipo_evento, payload)

        def _trabajo(tx):
            for sentencia in sentencias:
                tx.run(sentencia, **payload).consume()

        with _neo4j_disponible(), self._cx.neo4j.session() as s:
            s.execute_write(_trabajo)

    def ruta_envio(self, id_envio: int) -> list[dict]:
        with _neo4j_disponible(), self._cx.neo4j.session() as s:
            return [r.data() for r in s.run(RUTA_ENVIO, id_envio=id_envio)]

    def rutas_problematicas(self, limite: int = 10) -> list[dict]:
        with _neo4j_disponible(), self._cx.neo4j.session() as s:
            return [r.data() for r in s.run(RUTAS_PROBLEMATICAS, limite=limite)]

    def ruta_alternativa(self, desde: str, hasta: str, tasa_maxima: float = 0.2) -> dict | None:
        with _neo4j_disponible(), self._cx.neo4j.session() as s:
            fila = s.run(RUTA_ALTERNATIVA, desde=desde, hasta=hasta, tasa_maxima=tasa_maxima).single()
        return fila.data() if fila else None

    def estadisticas_rutas(self, rutas: list[dict]) -> dict[tuple, dict]:
        """{(origen, destino, medio): {envios, incidentes, demora_h}} de las rutas pedidas."""
        if not rutas:
            return {}
        with _neo4j_disponible(), self._cx.neo4j.session() as s:
            return {(f["origen"], f["destino"], f["medio"]): f.data()
                    for f in s.run(ESTADISTICAS_RUTAS, rutas=rutas)}

    def historial_contenedor(self, id_contenedor: int) -> list[dict]:
        with _neo4j_disponible(), self._cx.neo4j.session() as s:
            filas = [r.data() for r in s.run(HISTORIAL_CONTENEDOR, id_contenedor=id_contenedor)]
        for f in filas:                     # las fechas de Neo4j (neo4j.time.DateTime) pasan a datetime de Python
            for k in ("desde", "hasta"):
                if f[k] is not None:
                    f[k] = f[k].to_native()
        return filas

    def red_cliente(self, id_cliente: int) -> list[dict]:
        with _neo4j_disponible(), self._cx.neo4j.session() as s:
            return [r.data() for r in s.run(RED_CLIENTE, id_cliente=id_cliente)]

    def estados_envios(self) -> dict[int, str]:
        """{id_envio: estado} de todos los envíos del grafo (verificador de la fase 8)."""
        with _neo4j_disponible(), self._cx.neo4j.session() as s:
            return {r["id_envio"]: r["estado"] for r in s.run(ESTADOS_ENVIOS)}

    def contar_incidentes(self) -> int:
        with _neo4j_disponible(), self._cx.neo4j.session() as s:
            return s.run(CONTAR_INCIDENTES).single()["n"]

    def resumen(self) -> list[dict]:
        with _neo4j_disponible(), self._cx.neo4j.session() as s:
            return [r.data() for r in s.run(RESUMEN)]

    def vaciar(self) -> None:
        """Borra todos los nodos y relaciones (las restricciones e índices quedan)."""
        with _neo4j_disponible(), self._cx.neo4j.session() as s:
            s.run("MATCH (n) DETACH DELETE n").consume()
