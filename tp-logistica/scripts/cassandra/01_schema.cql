
-- =====================================================================
-- Cassandra — keyspace logistica_iot (telemetría de sensores)
-- Script de creación. Ejecutar con:  cqlsh -f scripts/cassandra/01_schema.cql
-- Modelado por consultas: una tabla por consulta (Q1..Q5).
-- =====================================================================

CREATE KEYSPACE IF NOT EXISTS logistica_iot
  WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 1};
  -- Producción: {'class': 'NetworkTopologyStrategy', 'dc1': 3}

USE logistica_iot;

-- Q1. Lecturas de un sensor en un día (o rango de horas de ese día), más recientes primero
CREATE TABLE IF NOT EXISTS lecturas_por_sensor (
  id_sensor      int,
  dia            date,
  fecha_hora     timestamp,
  id_contenedor  int,
  temperatura    decimal,
  humedad        decimal,
  vibracion      decimal,
  latitud        double,
  longitud       double,
  bateria        tinyint,
  PRIMARY KEY ((id_sensor, dia), fecha_hora)
) WITH CLUSTERING ORDER BY (fecha_hora DESC)
  AND default_time_to_live = 63072000
  AND compaction = {'class': 'TimeWindowCompactionStrategy',
                    'compaction_window_unit': 'DAYS', 'compaction_window_size': 1};

-- Q2. Lecturas de todos los sensores de un contenedor en un día
CREATE TABLE IF NOT EXISTS lecturas_por_contenedor (
  id_contenedor  int,
  dia            date,
  fecha_hora     timestamp,
  id_sensor      int,
  temperatura    decimal,
  humedad        decimal,
  vibracion      decimal,
  latitud        double,
  longitud       double,
  bateria        tinyint,
  PRIMARY KEY ((id_contenedor, dia), fecha_hora, id_sensor)
) WITH CLUSTERING ORDER BY (fecha_hora DESC, id_sensor ASC)
  AND default_time_to_live = 63072000
  AND compaction = {'class': 'TimeWindowCompactionStrategy',
                    'compaction_window_unit': 'DAYS', 'compaction_window_size': 1};

-- Q3. Posiciones GPS de un envío en orden cronológico (mapa y recorrido)
CREATE TABLE IF NOT EXISTS posiciones_por_envio (
  id_envio       int,
  fecha_hora     timestamp,
  id_contenedor  int,
  latitud        double,
  longitud       double,
  PRIMARY KEY (id_envio, fecha_hora, id_contenedor)
) WITH CLUSTERING ORDER BY (fecha_hora ASC, id_contenedor ASC);

-- Q4. Última lectura de un sensor (una fila por sensor, se sobrescribe)
CREATE TABLE IF NOT EXISTS ultima_lectura_por_sensor (
  id_sensor      int PRIMARY KEY,
  id_contenedor  int,
  fecha_hora     timestamp,
  temperatura    decimal,
  humedad        decimal,
  bateria        tinyint,
  latitud        double,
  longitud       double
);

-- Q5. Anomalías de temperatura de un día, más recientes primero
CREATE TABLE IF NOT EXISTS anomalias_temperatura (
  dia            date,
  fecha_hora     timestamp,
  id_sensor      int,
  id_contenedor  int,
  id_envio       int,
  temperatura    decimal,
  umbral_min     decimal,
  umbral_max     decimal,
  PRIMARY KEY (dia, fecha_hora, id_sensor)
) WITH CLUSTERING ORDER BY (fecha_hora DESC, id_sensor ASC)
  AND default_time_to_live = 31536000;
