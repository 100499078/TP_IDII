-- =====================================================================
-- Fase 9: cambios de esquema posteriores a 01_schema.sql.
-- Es IDEMPOTENTE (IF NOT EXISTS, ON CONFLICT): herramientas.inicializar_bases lo corre
-- siempre, así que sirve tanto para una base nueva como para una que ya tiene datos.
-- =====================================================================
SET search_path TO logistica, public;

-- Reglas de alerta configurables (antes eran constantes en el código).
-- TEMPERATURA no tiene umbral propio: usa el rango mínimo/máximo de cada sensor.
CREATE TABLE IF NOT EXISTS regla_alerta (
  tipo                VARCHAR(20) PRIMARY KEY
                      CHECK (tipo IN ('TEMPERATURA','HUMEDAD','VIBRACION','BATERIA_BAJA','SENSOR_INACTIVO')),
  umbral              NUMERIC(8,2) CHECK (umbral IS NULL OR umbral > 0),
  unidad              VARCHAR(10),
  activa              BOOLEAN NOT NULL DEFAULT true,
  descripcion         VARCHAR(200) NOT NULL,
  fecha_modificacion  TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK ((tipo = 'TEMPERATURA') = (umbral IS NULL))
);
INSERT INTO regla_alerta (tipo, umbral, unidad, descripcion) VALUES
  ('TEMPERATURA',     NULL, '°C',  'Lectura fuera del rango mínimo/máximo configurado en cada sensor'),
  ('HUMEDAD',         85,   '%',   'Humedad por encima del umbral: la carga corre riesgo'),
  ('VIBRACION',       2.5,  'g',   'Golpe por encima del umbral'),
  ('BATERIA_BAJA',    15,   '%',   'Batería del sensor por debajo del umbral'),
  ('SENSOR_INACTIVO', 30,   'min', 'Sensor de un contenedor en viaje que no reporta hace más que el umbral')
ON CONFLICT (tipo) DO NOTHING;

-- Reportes FACTURABLES: un reporte pedido para un cliente se cobra como un ítem de su factura.
-- El reporte vive en MongoDB; acá se guarda su ID. El índice único impide cobrar dos veces el
-- mismo reporte (aunque la marca en MongoDB no se haya podido grabar).
ALTER TABLE item_factura ADD COLUMN IF NOT EXISTS id_reporte VARCHAR(24);
CREATE UNIQUE INDEX IF NOT EXISTS ux_item_factura_reporte ON item_factura (id_reporte) WHERE id_reporte IS NOT NULL;

-- Rol para los usuarios de operadores logísticos y proveedores (mensajería y su cuenta corriente)
INSERT INTO rol (nombre, nivel_permisos, descripcion)
VALUES ('SOCIO', 1, 'Operadores logísticos y proveedores: mensajería, su cuenta corriente y los envíos que operan')
ON CONFLICT (nombre) DO NOTHING;
