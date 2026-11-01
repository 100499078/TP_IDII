// =====================================================================
// Neo4j — consultas avanzadas del enunciado
// =====================================================================

// Q1. Ruta completa de un envío (tramos en orden, con operador y vehículo)
MATCH (e:Envio {id_envio: $id_envio})-[:TIENE_TRAMO]->(t:Tramo),
      (t)-[:DESDE]->(o:Ubicacion), (t)-[:HASTA]->(d:Ubicacion),
      (t)-[:OPERADO_POR]->(op:Operador)
OPTIONAL MATCH (t)-[:EN_VEHICULO]->(v:Vehiculo)
RETURN t.orden AS orden, o.codigo AS desde, d.codigo AS hasta, t.medio AS medio,
       op.razon_social AS operador, v.identificacion AS vehiculo,
       t.fecha_salida AS salida, t.fecha_llegada_estimada AS llegada_estimada
ORDER BY t.orden;

// Q1 bis. La misma ruta como un único camino de longitud variable
MATCH (e:Envio {id_envio: $id_envio})-[:TIENE_TRAMO]->(primero:Tramo {orden: 1})
MATCH p = (primero)-[:SIGUIENTE*0..]->(ultimo:Tramo)
WHERE NOT (ultimo)-[:SIGUIENTE]->()
RETURN [t IN nodes(p) | t.id_tramo] AS tramos, length(p) + 1 AS cantidad;

// Q2. Red de relaciones de un cliente: sus envíos y los operadores que los movieron
MATCH p = (c:Cliente {id_actor: $id_cliente})-[:SOLICITO]->(:Envio)
          -[:TIENE_TRAMO]->(:Tramo)-[:OPERADO_POR]->(:Operador)
RETURN p;

// Q2 bis. Clientes que comparten operadores (red cliente–operador–cliente)
MATCH (c1:Cliente)-[:SOLICITO]->(:Envio)-[:TIENE_TRAMO]->(:Tramo)-[:OPERADO_POR]->(o:Operador),
      (c2:Cliente)-[:SOLICITO]->(:Envio)-[:TIENE_TRAMO]->(:Tramo)-[:OPERADO_POR]->(o)
WHERE c1.id_actor < c2.id_actor
RETURN c1.razon_social AS cliente_1, c2.razon_social AS cliente_2,
       collect(DISTINCT o.razon_social) AS operadores_en_comun
ORDER BY size(operadores_en_comun) DESC;

// Q3. Historial completo de movimientos de un contenedor
MATCH (k:Contenedor {codigo: $codigo})-[v:VIAJA_EN]->(e:Envio)-[:TIENE_TRAMO]->(t:Tramo),
      (t)-[:DESDE]->(o:Ubicacion), (t)-[:HASTA]->(d:Ubicacion)
RETURN e.id_envio AS envio, t.orden AS orden, o.codigo AS desde, d.codigo AS hasta,
       t.medio AS medio, t.fecha_salida AS salida
ORDER BY t.fecha_salida;

// Q4. Ranking de rutas con más incidentes
MATCH (a:Ubicacion)-[r:RUTA]->(b:Ubicacion)
WHERE r.envios > 0
RETURN a.codigo + ' → ' + b.codigo AS ruta, r.medio AS medio, r.incidentes AS incidentes,
       r.envios AS envios, round(toFloat(r.incidentes) / r.envios, 2) AS tasa
ORDER BY incidentes DESC, tasa DESC
LIMIT 10;

// Q5. Ruta alternativa entre dos ubicaciones evitando rutas con muchos incidentes
MATCH (a:Ubicacion {codigo: $desde}), (b:Ubicacion {codigo: $hasta})
MATCH p = shortestPath((a)-[:RUTA*..6]->(b))
WHERE all(r IN relationships(p) WHERE toFloat(r.incidentes) / r.envios < 0.2)
RETURN [u IN nodes(p) | u.codigo] AS escalas, length(p) AS tramos;
