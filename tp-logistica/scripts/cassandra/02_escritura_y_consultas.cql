-- =====================================================================
-- Cassandra — cómo se escribe una lectura y cómo se consulta cada tabla
-- =====================================================================
USE logistica_iot;

-- Escritura de UNA lectura: se copia en todas las tablas que la necesitan.
-- BATCH con registro (logged): si una escritura falla, Cassandra la reintenta
-- hasta completar el lote, así las copias no quedan desparejas.
BEGIN BATCH
  INSERT INTO lecturas_por_sensor (id_sensor, dia, fecha_hora, id_contenedor, temperatura, humedad, vibracion, latitud, longitud, bateria)
  VALUES (88, '2026-10-06', '2026-10-06 14:32:00+0000', 12, 9.4, 71.0, 0.02, 39.45, -0.32, 87);
  INSERT INTO lecturas_por_contenedor (id_contenedor, dia, fecha_hora, id_sensor, temperatura, humedad, vibracion, latitud, longitud, bateria)
  VALUES (12, '2026-10-06', '2026-10-06 14:32:00+0000', 88, 9.4, 71.0, 0.02, 39.45, -0.32, 87);
  INSERT INTO posiciones_por_envio (id_envio, fecha_hora, id_contenedor, latitud, longitud)
  VALUES (1042, '2026-10-06 14:32:00+0000', 12, 39.45, -0.32);
  INSERT INTO ultima_lectura_por_sensor (id_sensor, id_contenedor, fecha_hora, temperatura, humedad, bateria, latitud, longitud)
  VALUES (88, 12, '2026-10-06 14:32:00+0000', 9.4, 71.0, 87, 39.45, -0.32);
  INSERT INTO anomalias_temperatura (dia, fecha_hora, id_sensor, id_contenedor, id_envio, temperatura, umbral_min, umbral_max)
  VALUES ('2026-10-06', '2026-10-06 14:32:00+0000', 88, 12, 1042, 9.4, 2.0, 5.0);
APPLY BATCH;

-- Q1. Lecturas del sensor 88 el 6/10 entre las 12 y las 18 h
SELECT fecha_hora, temperatura, humedad, bateria
FROM lecturas_por_sensor
WHERE id_sensor = 88 AND dia = '2026-10-06'
  AND fecha_hora >= '2026-10-06 12:00:00+0000' AND fecha_hora < '2026-10-06 18:00:00+0000';

-- Q2. Lecturas del contenedor 12 el 6/10
SELECT fecha_hora, id_sensor, temperatura, humedad
FROM lecturas_por_contenedor
WHERE id_contenedor = 12 AND dia = '2026-10-06';

-- Q3. Recorrido GPS del envío 1042, en orden
SELECT fecha_hora, latitud, longitud
FROM posiciones_por_envio
WHERE id_envio = 1042;

-- Q4. Estado actual del sensor 88
SELECT fecha_hora, temperatura, bateria
FROM ultima_lectura_por_sensor
WHERE id_sensor = 88;

-- Q5. Anomalías de temperatura del 6/10
SELECT fecha_hora, id_sensor, id_envio, temperatura, umbral_max
FROM anomalias_temperatura
WHERE dia = '2026-10-06';
