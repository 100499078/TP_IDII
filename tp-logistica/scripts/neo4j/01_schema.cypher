// =====================================================================
// Neo4j — grafo de la red logística (proyección de PostgreSQL y MongoDB)
// Script de creación: constraints de unicidad + índices.
// Ejecutar con:  cypher-shell -u neo4j -p <clave> -f scripts/neo4j/01_schema.cypher
// =====================================================================

// ---------- Unicidad de la clave de negocio (cada una crea su índice) ----------
CREATE CONSTRAINT cliente_id    IF NOT EXISTS FOR (n:Cliente)    REQUIRE n.id_actor IS UNIQUE;
CREATE CONSTRAINT operador_id   IF NOT EXISTS FOR (n:Operador)   REQUIRE n.id_actor IS UNIQUE;
CREATE CONSTRAINT envio_id      IF NOT EXISTS FOR (n:Envio)      REQUIRE n.id_envio IS UNIQUE;
CREATE CONSTRAINT tramo_id      IF NOT EXISTS FOR (n:Tramo)      REQUIRE n.id_tramo IS UNIQUE;
CREATE CONSTRAINT contenedor_id IF NOT EXISTS FOR (n:Contenedor) REQUIRE n.id_contenedor IS UNIQUE;
CREATE CONSTRAINT vehiculo_id   IF NOT EXISTS FOR (n:Vehiculo)   REQUIRE n.id_vehiculo IS UNIQUE;
CREATE CONSTRAINT ubicacion_cod IF NOT EXISTS FOR (n:Ubicacion)  REQUIRE n.codigo IS UNIQUE;
CREATE CONSTRAINT pais_cod      IF NOT EXISTS FOR (n:Pais)       REQUIRE n.codigo IS UNIQUE;
CREATE CONSTRAINT incidente_id  IF NOT EXISTS FOR (n:Incidente)  REQUIRE n.id_incidente IS UNIQUE;   // fase 4

// ---------- Índices de búsqueda por propiedad ----------
CREATE INDEX envio_estado       IF NOT EXISTS FOR (n:Envio)       ON (n.estado);
CREATE INDEX contenedor_codigo  IF NOT EXISTS FOR (n:Contenedor)  ON (n.codigo);
CREATE INDEX tramo_medio        IF NOT EXISTS FOR (n:Tramo)       ON (n.medio);
CREATE INDEX ruta_incidentes    IF NOT EXISTS FOR ()-[r:RUTA]-()      ON (r.incidentes);
CREATE INDEX viaja_en_desde     IF NOT EXISTS FOR ()-[r:VIAJA_EN]-()  ON (r.desde);
