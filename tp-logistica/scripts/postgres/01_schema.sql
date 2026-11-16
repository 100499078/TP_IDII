CREATE SCHEMA logistica;
SET search_path TO logistica;
-- Para que psql y cualquier cliente vean las tablas sin escribir 'logistica.' delante
ALTER DATABASE logistica SET search_path TO logistica, public;

-- ===== Actores comerciales (supertipo + subtipos) =====
CREATE TABLE actor (
  id_actor      SERIAL PRIMARY KEY,
  tipo          VARCHAR(10)  NOT NULL CHECK (tipo IN ('CLIENTE','OPERADOR','PROVEEDOR')),
  razon_social  VARCHAR(150) NOT NULL,
  cuit          CHAR(13)     NOT NULL UNIQUE CHECK (cuit ~ '^\d{2}-\d{8}-\d$'),
  email         VARCHAR(120) NOT NULL UNIQUE,
  telefono      VARCHAR(30),
  direccion     VARCHAR(200),
  pais          CHAR(2)      NOT NULL,
  estado        VARCHAR(10)  NOT NULL DEFAULT 'ACTIVO'
                CHECK (estado IN ('ACTIVO','INACTIVO','SUSPENDIDO')),
  fecha_alta    TIMESTAMPTZ  NOT NULL DEFAULT now()
);
-- Subtipos
CREATE TABLE cliente (
  id_actor            INT PRIMARY KEY REFERENCES actor ON DELETE CASCADE,
  limite_credito      NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (limite_credito >= 0),
  condicion_pago_dias SMALLINT NOT NULL DEFAULT 30 CHECK (condicion_pago_dias BETWEEN 0 AND 180)
);
CREATE TABLE operador_logistico (
  id_actor  INT PRIMARY KEY REFERENCES actor ON DELETE CASCADE,
  medios    VARCHAR(10)[] NOT NULL
);
CREATE TABLE proveedor (
  id_actor  INT PRIMARY KEY REFERENCES actor ON DELETE CASCADE,
  rubro     VARCHAR(60) NOT NULL
);

-- ===== Seguridad =====
CREATE TABLE rol (
  id_rol          SERIAL PRIMARY KEY,
  nombre          VARCHAR(30) NOT NULL UNIQUE,
  descripcion     VARCHAR(200),
  nivel_permisos  SMALLINT NOT NULL CHECK (nivel_permisos BETWEEN 1 AND 5)
);
CREATE TABLE usuario (
  id_usuario      SERIAL PRIMARY KEY,
  id_actor        INT REFERENCES actor,
  nombre          VARCHAR(60)  NOT NULL,
  apellido        VARCHAR(60)  NOT NULL,
  email           VARCHAR(120) NOT NULL UNIQUE,
  password_hash   VARCHAR(100) NOT NULL,
  estado          VARCHAR(10)  NOT NULL DEFAULT 'ACTIVO'
                  CHECK (estado IN ('ACTIVO','BLOQUEADO','BAJA')),
  fecha_registro  TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE TABLE usuario_rol (
  id_usuario INT REFERENCES usuario ON DELETE CASCADE,
  id_rol     INT REFERENCES rol,
  PRIMARY KEY (id_usuario, id_rol)
);
CREATE TABLE sesion_historial (               -- auditoría: una fila por sesión (la sesión viva está en Redis)
  id_sesion     UUID PRIMARY KEY,
  id_usuario    INT NOT NULL REFERENCES usuario,
  fecha_inicio  TIMESTAMPTZ NOT NULL DEFAULT now(),
  fecha_fin     TIMESTAMPTZ,
  estado        VARCHAR(10) NOT NULL DEFAULT 'ACTIVA'
                CHECK (estado IN ('ACTIVA','CERRADA','EXPIRADA','REVOCADA')),
  CHECK (fecha_fin IS NULL OR fecha_fin >= fecha_inicio),
  CHECK ((estado = 'ACTIVA') = (fecha_fin IS NULL))
);

-- ===== Catálogos =====
CREATE TABLE ubicacion (
  id_ubicacion  SERIAL PRIMARY KEY,
  codigo        VARCHAR(10) NOT NULL UNIQUE,
  nombre        VARCHAR(100) NOT NULL,
  tipo          VARCHAR(15) NOT NULL CHECK (tipo IN ('PUERTO','AEROPUERTO','CENTRO_DIST','CIUDAD')),
  pais          CHAR(2) NOT NULL,
  latitud       NUMERIC(9,6) NOT NULL CHECK (latitud  BETWEEN -90  AND 90),
  longitud      NUMERIC(9,6) NOT NULL CHECK (longitud BETWEEN -180 AND 180)
);
CREATE TABLE contenedor (
  id_contenedor        SERIAL PRIMARY KEY,
  codigo_internacional CHAR(11) NOT NULL UNIQUE CHECK (codigo_internacional ~ '^[A-Z]{4}\d{7}$'),
  tipo                 VARCHAR(10) NOT NULL CHECK (tipo IN ('DRY','REEFER','OPEN_TOP','TANK','FLAT_RACK')),
  capacidad_m3         NUMERIC(6,2) NOT NULL CHECK (capacidad_m3 > 0),
  estado               VARCHAR(15) NOT NULL DEFAULT 'DISPONIBLE'
                       CHECK (estado IN ('DISPONIBLE','EN_USO','MANTENIMIENTO','BAJA'))
);
CREATE TABLE sensor (
  id_sensor          SERIAL PRIMARY KEY,
  id_contenedor      INT REFERENCES contenedor,
  tipo               VARCHAR(12) NOT NULL CHECK (tipo IN ('TEMPERATURA','HUMEDAD','VIBRACION','GPS','MULTI')),
  fabricante         VARCHAR(60) NOT NULL,
  modelo             VARCHAR(60),
  fecha_instalacion  DATE,
  estado             VARCHAR(10) NOT NULL DEFAULT 'ACTIVO'
                     CHECK (estado IN ('ACTIVO','INACTIVO','FALLA','BAJA')),
  temp_min           NUMERIC(5,2),
  temp_max           NUMERIC(5,2),
  CHECK (temp_min IS NULL OR temp_max IS NULL OR temp_min < temp_max),
  CHECK (id_contenedor IS NULL OR fecha_instalacion IS NOT NULL)
);
CREATE TABLE vehiculo (
  id_vehiculo     SERIAL PRIMARY KEY,
  tipo            VARCHAR(10) NOT NULL CHECK (tipo IN ('CAMION','TREN','BUQUE','AVION')),
  identificacion  VARCHAR(20) NOT NULL UNIQUE,
  id_operador     INT NOT NULL REFERENCES operador_logistico,
  estado          VARCHAR(15) NOT NULL DEFAULT 'OPERATIVO'
                  CHECK (estado IN ('OPERATIVO','MANTENIMIENTO','BAJA'))
);
CREATE TABLE tarifa (
  id_tarifa         SERIAL PRIMARY KEY,
  medio_transporte  VARCHAR(10) NOT NULL CHECK (medio_transporte IN ('TERRESTRE','MARITIMO','AEREO')),
  precio_base       NUMERIC(12,2) NOT NULL CHECK (precio_base >= 0),
  precio_kg         NUMERIC(10,4) NOT NULL CHECK (precio_kg >= 0),
  vigente_desde     DATE NOT NULL,
  vigente_hasta     DATE,
  UNIQUE (medio_transporte, vigente_desde),
  CHECK (vigente_hasta IS NULL OR vigente_hasta > vigente_desde)
);

-- ===== Operación =====
CREATE TABLE envio (
  id_envio                SERIAL PRIMARY KEY,
  id_cliente              INT NOT NULL REFERENCES cliente,
  id_origen               INT NOT NULL REFERENCES ubicacion,
  id_destino              INT NOT NULL REFERENCES ubicacion,
  fecha_creacion          TIMESTAMPTZ NOT NULL DEFAULT now(),
  fecha_entrega_estimada  TIMESTAMPTZ NOT NULL,
  fecha_entrega_real      TIMESTAMPTZ,
  estado      VARCHAR(12) NOT NULL DEFAULT 'CREADO'
              CHECK (estado IN ('CREADO','EN_TRANSITO','EN_ADUANA','DEMORADO','ENTREGADO','CANCELADO')),
  prioridad   VARCHAR(8)  NOT NULL DEFAULT 'MEDIA' CHECK (prioridad IN ('BAJA','MEDIA','ALTA','URGENTE')),
  peso_kg     NUMERIC(10,2) NOT NULL CHECK (peso_kg > 0),
  volumen_m3  NUMERIC(8,2)  NOT NULL CHECK (volumen_m3 > 0),
  CHECK (id_origen <> id_destino),
  CHECK (fecha_entrega_estimada > fecha_creacion),
  CHECK ((estado = 'ENTREGADO') = (fecha_entrega_real IS NOT NULL))
);
CREATE TABLE envio_contenedor (
  id_envio       INT REFERENCES envio ON DELETE CASCADE,
  id_contenedor  INT REFERENCES contenedor,
  PRIMARY KEY (id_envio, id_contenedor)
);
CREATE TABLE tramo (
  id_tramo                SERIAL PRIMARY KEY, -- Numero que se pone solo, una secuencia
  id_envio                INT NOT NULL REFERENCES envio ON DELETE CASCADE,
  orden                   SMALLINT NOT NULL CHECK (orden >= 1),
  medio_transporte        VARCHAR(10) NOT NULL CHECK (medio_transporte IN ('TERRESTRE','MARITIMO','AEREO')),
  id_origen               INT NOT NULL REFERENCES ubicacion,
  id_destino              INT NOT NULL REFERENCES ubicacion,
  id_operador             INT NOT NULL REFERENCES operador_logistico,
  id_vehiculo             INT REFERENCES vehiculo,
  fecha_salida            TIMESTAMPTZ NOT NULL,
  fecha_llegada_estimada  TIMESTAMPTZ NOT NULL,
  fecha_llegada_real      TIMESTAMPTZ,
  costo                   NUMERIC(12,2) NOT NULL DEFAULT 0 CHECK (costo >= 0),
  UNIQUE (id_envio, orden),
  CHECK (id_origen <> id_destino),
  CHECK (fecha_llegada_estimada > fecha_salida)
);

-- ===== Comercial =====
CREATE TABLE factura (
  id_factura         SERIAL PRIMARY KEY,
  id_actor           INT NOT NULL REFERENCES actor,
  tipo               VARCHAR(8) NOT NULL CHECK (tipo IN ('EMITIDA','RECIBIDA')),
  numero             VARCHAR(20) NOT NULL,
  fecha_emision      DATE NOT NULL DEFAULT current_date,
  fecha_vencimiento  DATE NOT NULL,
  importe_total      NUMERIC(14,2) NOT NULL CHECK (importe_total >= 0),
  estado             VARCHAR(10) NOT NULL DEFAULT 'PENDIENTE'
                     CHECK (estado IN ('PENDIENTE','PARCIAL','PAGADA','VENCIDA','ANULADA')),
  UNIQUE (tipo, numero),
  CHECK (fecha_vencimiento >= fecha_emision)
);
CREATE TABLE item_factura (
  id_item          SERIAL PRIMARY KEY,
  id_factura       INT NOT NULL REFERENCES factura ON DELETE CASCADE,
  id_envio         INT REFERENCES envio,
  descripcion      VARCHAR(200) NOT NULL,
  cantidad         NUMERIC(10,2) NOT NULL CHECK (cantidad > 0),
  precio_unitario  NUMERIC(12,2) NOT NULL CHECK (precio_unitario >= 0),
  subtotal         NUMERIC(14,2) GENERATED ALWAYS AS (cantidad * precio_unitario) STORED
);
CREATE TABLE pago (
  id_pago     SERIAL PRIMARY KEY,
  id_factura  INT NOT NULL REFERENCES factura,
  fecha       TIMESTAMPTZ NOT NULL DEFAULT now(),
  monto       NUMERIC(14,2) NOT NULL CHECK (monto > 0),
  medio_pago  VARCHAR(15) NOT NULL CHECK (medio_pago IN ('TRANSFERENCIA','TARJETA','CHEQUE','EFECTIVO'))
);
CREATE TABLE cuenta_corriente (
  id_cuenta            SERIAL PRIMARY KEY,
  id_actor             INT NOT NULL UNIQUE REFERENCES actor,
  saldo                NUMERIC(14,2) NOT NULL DEFAULT 0,
  fecha_actualizacion  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE movimiento_cc (
  id_movimiento  BIGSERIAL PRIMARY KEY,
  id_cuenta      INT NOT NULL REFERENCES cuenta_corriente,
  fecha          TIMESTAMPTZ NOT NULL DEFAULT now(),
  tipo           VARCHAR(7) NOT NULL CHECK (tipo IN ('DEBITO','CREDITO')),
  importe        NUMERIC(14,2) NOT NULL CHECK (importe > 0),
  id_factura     INT REFERENCES factura,
  id_pago        INT REFERENCES pago,
  descripcion    VARCHAR(200),
  CHECK (num_nonnulls(id_factura, id_pago) <= 1)
);
CREATE TABLE cierre_mensual (
  id_cuenta      INT REFERENCES cuenta_corriente,
  periodo        CHAR(7) CHECK (periodo ~ '^\d{4}-\d{2}$'),
  saldo_inicial  NUMERIC(14,2) NOT NULL,
  total_debitos  NUMERIC(14,2) NOT NULL,
  total_creditos NUMERIC(14,2) NOT NULL,
  saldo_final    NUMERIC(14,2) NOT NULL,
  fecha_cierre   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (id_cuenta, periodo),
  CHECK (saldo_final = saldo_inicial + total_debitos - total_creditos)
);

-- ===== Sincronización (transactional outbox) =====
CREATE TABLE outbox (
  id_evento     BIGSERIAL PRIMARY KEY,
  agregado      VARCHAR(30) NOT NULL,
  id_agregado   INT NOT NULL,
  tipo_evento   VARCHAR(40) NOT NULL,
  payload       JSONB NOT NULL,
  creado_en     TIMESTAMPTZ NOT NULL DEFAULT now(),
  procesado_en  TIMESTAMPTZ
);

-- ===== Índices =====
-- índice compuesto: (A, B) sirve para buscar por A, o por A y B, pero no por B sola.
-- índice parcial: solo guarda algunas filas, las que cumplen la condición WHERE.
-- índice único: prohibe valores repetidos
CREATE INDEX ix_envio_cliente_fecha ON envio (id_cliente, fecha_creacion); -- buscar los envíos de un cliente
CREATE INDEX ix_envio_estado        ON envio (estado); -- filtrar envios por estado, más útil con los estados minoritarios
CREATE INDEX ix_envio_demorados     ON envio (fecha_entrega_estimada)
  WHERE estado NOT IN ('ENTREGADO','CANCELADO'); -- encontrar rápido los envíos que ya pasaron su fecha estimada
CREATE INDEX ix_ubicacion_pais      ON ubicacion (pais);
CREATE INDEX ix_tramo_medio         ON tramo (medio_transporte); -- poco selectivo
CREATE INDEX ix_sensor_contenedor   ON sensor (id_contenedor); -- sobre una FK
CREATE INDEX ix_factura_actor_estado ON factura (id_actor, estado);
CREATE UNIQUE INDEX ux_item_factura_envio ON item_factura (id_envio) WHERE id_envio IS NOT NULL; -- un envío no puede facturar dos veces
CREATE INDEX ix_pago_factura        ON pago (id_factura);
CREATE INDEX ix_movimiento_cuenta_fecha ON movimiento_cc (id_cuenta, fecha);
CREATE INDEX ix_outbox_pendientes   ON outbox (creado_en) WHERE procesado_en IS NULL;
CREATE INDEX ix_sesiones_activas   ON sesion_historial (id_usuario) WHERE estado = 'ACTIVA';

-- ===== Trigger: saldo de cuenta corriente =====
-- Así es imposible insertar un movimiento sin actualizar el saldo
CREATE FUNCTION fn_actualizar_saldo() RETURNS trigger AS $$
BEGIN
  UPDATE cuenta_corriente
     SET saldo = saldo + CASE NEW.tipo WHEN 'DEBITO' THEN NEW.importe ELSE -NEW.importe END,
         fecha_actualizacion = now()
   WHERE id_cuenta = NEW.id_cuenta;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_movimiento_saldo
AFTER INSERT ON movimiento_cc
FOR EACH ROW EXECUTE FUNCTION fn_actualizar_saldo();
