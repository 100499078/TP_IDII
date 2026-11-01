// =====================================================================
// Neo4j — sentencias idempotentes que aplica el sincronizador.
// Es una COPIA de referencia de app/repositorios/neo4j/grafo.py (SENTENCIAS),
// que es la que ejecuta la aplicación. Cada sentencia recibe los parámetros
// ($...) del payload del evento (outbox de PostgreSQL, o incidentes de MongoDB
// desde la fase 4). Todas usan MERGE: reprocesar un evento no duplica nada.
// =====================================================================

// ACTOR_GUARDADO_CLIENTE
MERGE (c:Cliente {id_actor: $id_actor})
SET c.razon_social = $razon_social, c.pais = $pais, c.estado = $estado
WITH c
OPTIONAL MATCH (c)-[viejo:EN_PAIS]->(:Pais)
DELETE viejo
WITH c
MERGE (p:Pais {codigo: $pais})
MERGE (c)-[:EN_PAIS]->(p);

// ACTOR_GUARDADO_OPERADOR
MERGE (o:Operador {id_actor: $id_actor})
SET o.razon_social = $razon_social, o.pais = $pais, o.estado = $estado;

// UBICACION_GUARDADA
MERGE (u:Ubicacion {codigo: $codigo})
SET u.nombre = $nombre, u.tipo = $tipo,
    u.punto = point({latitude: $latitud, longitude: $longitud})
WITH u
OPTIONAL MATCH (u)-[viejo:EN_PAIS]->(:Pais)
DELETE viejo
WITH u
MERGE (p:Pais {codigo: $pais})
MERGE (u)-[:EN_PAIS]->(p);

// CONTENEDOR_GUARDADO
MERGE (k:Contenedor {id_contenedor: $id_contenedor})
SET k.codigo = $codigo, k.tipo = $tipo;

// VEHICULO_GUARDADO
MERGE (v:Vehiculo {id_vehiculo: $id_vehiculo})
SET v.tipo = $tipo, v.identificacion = $identificacion
MERGE (o:Operador {id_actor: $id_operador})
MERGE (v)-[:PERTENECE_A]->(o);

// ENVIO_CREADO
MERGE (e:Envio {id_envio: $id_envio})
SET e.estado = $estado, e.prioridad = $prioridad, e.origen = $origen, e.destino = $destino,
    e.fecha_creacion = datetime($fecha_creacion)
MERGE (c:Cliente {id_actor: $id_cliente})
MERGE (c)-[:SOLICITO]->(e)
WITH e
UNWIND $contenedores AS k
MERGE (n:Contenedor {id_contenedor: k.id_contenedor})
MERGE (n)-[v:VIAJA_EN]->(e)
SET v.desde = datetime(k.desde);

// ENVIO_ESTADO
MERGE (e:Envio {id_envio: $id_envio})
SET e.estado = $estado
WITH e
UNWIND $contenedores_liberados AS id_k
MATCH (:Contenedor {id_contenedor: id_k})-[v:VIAJA_EN]->(e)
SET v.hasta = datetime($fecha);

// TRAMO_AGREGADO (parte 1)
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
FOREACH (_ IN CASE WHEN anterior IS NULL THEN [] ELSE [1] END | MERGE (anterior)-[:SIGUIENTE]->(t));

// TRAMO_AGREGADO (parte 2)
MATCH (t:Tramo {id_tramo: $id_tramo})
WITH t WHERE $id_vehiculo IS NOT NULL
MERGE (v:Vehiculo {id_vehiculo: $id_vehiculo})
MERGE (t)-[:EN_VEHICULO]->(v);

// TRAMO_LLEGADA
MATCH (t:Tramo {id_tramo: $id_tramo})-[:DESDE]->(o:Ubicacion),
      (t)-[:HASTA]->(d:Ubicacion),
      (o)-[ru:RUTA {medio: t.medio}]->(d)
WITH t, ru, t.fecha_llegada_real IS NULL AS primera,
     coalesce(ru.llegadas, 0) AS n, coalesce(ru.demora_prom_h, 0.0) AS promedio
SET t.fecha_llegada_real = datetime($fecha)
// promedio móvil de la demora de la ruta; solo la primera vez que se registra la llegada
FOREACH (_ IN CASE WHEN primera THEN [1] ELSE [] END |
  SET ru.llegadas = n + 1, ru.demora_prom_h = (promedio * n + $demora_h) / (n + 1));

// INCIDENTE_REGISTRADO
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
SET ru.incidentes = ru.incidentes + CASE WHEN nuevo THEN 1 ELSE 0 END;
