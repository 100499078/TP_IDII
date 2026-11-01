// =====================================================================
// MongoDB — base logistica_docs
// Script de creación: colecciones con validador $jsonSchema + índices.
// Ejecutar con:  mongosh "mongodb://localhost:27017" scripts/mongo/01_schema.js
// =====================================================================

db = db.getSiblingDB("logistica_docs");

// Tipos reutilizados
const ENTERO = { bsonType: ["int", "long"] };          // ids que vienen de PostgreSQL
const NUMERO = { bsonType: ["int", "long", "double", "decimal"] };
const PAIS   = { bsonType: "string", pattern: "^[A-Z]{2}$" };     // ISO 3166-1 alfa-2
const MEDIO  = { enum: ["TERRESTRE", "MARITIMO", "AEREO"] };
const PUNTO  = {                                                  // GeoJSON Point
  bsonType: "object",
  required: ["type", "coordinates"],
  properties: {
    type: { enum: ["Point"] },
    coordinates: { bsonType: "array", minItems: 2, maxItems: 2, items: NUMERO }
  }
};

function crear(nombre, schema) {
  if (db.getCollectionNames().includes(nombre)) db.getCollection(nombre).drop();
  db.createCollection(nombre, {
    validator: { $jsonSchema: schema },
    validationLevel: "strict",
    validationAction: "error"
  });
}

// ---------------------------------------------------------------------
// 1. eventos_logisticos — historial de cada envío
// ---------------------------------------------------------------------
crear("eventos_logisticos", {
  bsonType: "object",
  required: ["id_envio", "tipo", "fecha_hora", "ubicacion"],
  properties: {
    id_envio:   ENTERO,
    tipo:       { enum: ["CARGA_INICIADA", "EN_TRANSITO", "ADUANA", "RETRASO", "DESCARGA", "ENTREGADO", "OTRO"] },
    fecha_hora: { bsonType: "date" },
    ubicacion: {
      bsonType: "object",
      required: ["codigo", "pais", "punto"],
      properties: {
        codigo: { bsonType: "string", maxLength: 10 },
        nombre: { bsonType: "string" },
        pais:   PAIS,
        punto:  PUNTO
      }
    },
    descripcion:    { bsonType: "string", maxLength: 500 },
    detalle:        { bsonType: "object" },                 // campos propios de cada tipo
    registrado_por: {
      bsonType: "object",
      properties: { id_usuario: ENTERO, nombre: { bsonType: "string" } }
    }
  }
});
db.eventos_logisticos.createIndex({ id_envio: 1, fecha_hora: 1 }, { name: "ix_envio_fecha" });
db.eventos_logisticos.createIndex({ tipo: 1, fecha_hora: -1 },     { name: "ix_tipo_fecha" });
db.eventos_logisticos.createIndex({ "ubicacion.punto": "2dsphere" }, { name: "ix_geo" });

// ---------------------------------------------------------------------
// 2. incidentes — con contexto embebido para los promedios y rankings
// ---------------------------------------------------------------------
crear("incidentes", {
  bsonType: "object",
  required: ["id_envio", "tipo", "fecha", "severidad", "estado", "contexto"],
  properties: {
    id_envio:    ENTERO,
    tipo:        { enum: ["TEMPERATURA_FUERA_DE_RANGO", "DANIO_CARGA", "DEMORA_ADUANA", "ROBO",
                          "CLIMA", "FALLA_VEHICULO", "DOCUMENTACION", "OTRO"] },
    fecha:       { bsonType: "date" },
    severidad:   { bsonType: ["int", "long"], minimum: 1, maximum: 5 },
    estado:      { enum: ["ABIERTO", "EN_ANALISIS", "RESUELTO", "CERRADO"] },
    descripcion: { bsonType: "string", maxLength: 1000 },
    contexto: {
      bsonType: "object",
      required: ["id_cliente", "pais_origen", "pais_destino", "ruta"],
      properties: {
        id_cliente:   ENTERO,
        pais_origen:  PAIS,
        pais_destino: PAIS,
        ruta:         { bsonType: "string", pattern: "^[A-Z0-9]{5}-[A-Z0-9]{5}$" },  // ARBUE-ESVLC
        id_tramo:     ENTERO,
        medio:        MEDIO
      }
    },
    detalle: { bsonType: "object" },
    historial_estados: {
      bsonType: "array",
      items: {
        bsonType: "object",
        required: ["estado", "fecha"],
        properties: {
          estado:     { enum: ["ABIERTO", "EN_ANALISIS", "RESUELTO", "CERRADO"] },
          fecha:      { bsonType: "date" },
          id_usuario: { bsonType: ["int", "long", "null"] }
        }
      }
    }
  }
});
db.incidentes.createIndex({ estado: 1, severidad: -1 },     { name: "ix_estado_severidad" });
db.incidentes.createIndex({ id_envio: 1 },                  { name: "ix_envio" });
db.incidentes.createIndex({ "contexto.ruta": 1 },           { name: "ix_ruta" });
db.incidentes.createIndex({ "contexto.id_cliente": 1 },     { name: "ix_cliente" });
db.incidentes.createIndex({ "contexto.pais_origen": 1 },    { name: "ix_pais_origen" });

// ---------------------------------------------------------------------
// 3. alertas — generadas por sensores o por eventos
// ---------------------------------------------------------------------
crear("alertas", {
  bsonType: "object",
  required: ["tipo", "fecha", "estado", "origen"],
  properties: {
    tipo:   { enum: ["TEMPERATURA_ALTA", "TEMPERATURA_BAJA", "HUMEDAD", "VIBRACION",
                     "BATERIA_BAJA", "SENSOR_INACTIVO", "RETRASO", "OTRO"] },
    fecha:  { bsonType: "date" },
    estado: { enum: ["NUEVA", "VISTA", "RESUELTA"] },
    origen: {
      bsonType: "object",
      properties: {                       // sensor: id_sensor + id_contenedor; evento: id_evento
        id_sensor:     ENTERO,
        id_contenedor: ENTERO,
        id_evento:     { bsonType: "objectId" }
      }
    },
    id_envio: { bsonType: ["int", "long", "null"] },
    valor:    NUMERO,
    umbral:   NUMERO,
    mensaje:  { bsonType: "string", maxLength: 500 }
  }
});
db.alertas.createIndex({ estado: 1, fecha: -1 },             { name: "ix_estado_fecha" });
db.alertas.createIndex({ "origen.id_sensor": 1, fecha: -1 }, { name: "ix_sensor_fecha" });
db.alertas.createIndex({ id_envio: 1 },                      { name: "ix_envio" });

// ---------------------------------------------------------------------
// 4. grupos_trabajo — integrantes embebidos (lista acotada)
// ---------------------------------------------------------------------
crear("grupos_trabajo", {
  bsonType: "object",
  required: ["nombre", "integrantes", "creado_por", "fecha_creacion", "activo"],
  properties: {
    nombre:      { bsonType: "string", minLength: 3, maxLength: 80 },
    descripcion: { bsonType: "string", maxLength: 500 },
    integrantes: {
      bsonType: "array",
      minItems: 1,
      maxItems: 200,
      items: {
        bsonType: "object",
        required: ["id_usuario", "rol_en_grupo", "desde"],
        properties: {
          id_usuario:   ENTERO,
          nombre:       { bsonType: "string" },
          rol_en_grupo: { enum: ["ADMIN", "MIEMBRO"] },
          desde:        { bsonType: "date" }
        }
      }
    },
    creado_por:     ENTERO,
    fecha_creacion: { bsonType: "date" },
    activo:         { bsonType: "bool" }
  }
});
db.grupos_trabajo.createIndex({ nombre: 1 },                  { name: "ux_nombre", unique: true });
db.grupos_trabajo.createIndex({ "integrantes.id_usuario": 1 }, { name: "ix_integrante" });

// ---------------------------------------------------------------------
// 5. conversaciones — cabecera de cada chat (privado o grupal)
// ---------------------------------------------------------------------
crear("conversaciones", {
  bsonType: "object",
  required: ["tipo", "participantes", "creada"],
  properties: {
    tipo:          { enum: ["PRIVADA", "GRUPAL"] },
    participantes: { bsonType: "array", minItems: 2, items: ENTERO, uniqueItems: true },
    id_grupo:      { bsonType: "objectId" },          // solo en conversaciones grupales
    creada:        { bsonType: "date" },
    ultimo_mensaje: {
      bsonType: "object",
      properties: {
        resumen:    { bsonType: "string", maxLength: 120 },
        fecha:      { bsonType: "date" },
        id_usuario: ENTERO
      }
    }
  }
});
db.conversaciones.createIndex({ participantes: 1, "ultimo_mensaje.fecha": -1 }, { name: "ix_bandeja" });
db.conversaciones.createIndex({ id_grupo: 1 }, { name: "ix_grupo", sparse: true });

// ---------------------------------------------------------------------
// 6. mensajes — colección aparte: crece sin límite
// ---------------------------------------------------------------------
crear("mensajes", {
  bsonType: "object",
  required: ["id_conversacion", "remitente", "destinatario", "fecha_hora", "contenido", "tipo"],
  properties: {
    id_conversacion: { bsonType: "objectId" },
    remitente: {
      bsonType: "object",
      required: ["id_usuario"],
      properties: { id_usuario: ENTERO, nombre: { bsonType: "string" } }
    },
    destinatario: {
      bsonType: "object",
      required: ["tipo"],
      properties: {
        tipo:       { enum: ["USUARIO", "GRUPO"] },
        id_usuario: ENTERO,                       // si tipo = USUARIO
        id_grupo:   { bsonType: "objectId" }      // si tipo = GRUPO
      }
    },
    fecha_hora: { bsonType: "date" },
    contenido:  { bsonType: "string", minLength: 1, maxLength: 4000 },
    tipo:       { enum: ["TEXTO", "ARCHIVO", "SISTEMA"] },
    adjunto: {
      bsonType: "object",
      properties: { nombre: { bsonType: "string" }, url: { bsonType: "string" }, bytes: NUMERO }
    },
    leido_por: { bsonType: "array", items: ENTERO }
  }
});
db.mensajes.createIndex({ id_conversacion: 1, fecha_hora: -1 }, { name: "ix_conversacion_fecha" });
db.mensajes.createIndex({ "remitente.id_usuario": 1, fecha_hora: -1 }, { name: "ix_remitente" });

// ---------------------------------------------------------------------
// 7. reportes — solicitud, estado y resultado
// ---------------------------------------------------------------------
crear("reportes", {
  bsonType: "object",
  required: ["id_usuario", "tipo", "parametros", "formato", "estado", "fecha_solicitud"],
  properties: {
    id_usuario: ENTERO,
    tipo: { enum: ["ENVIOS_POR_PAIS", "ENVIOS_POR_ESTADO", "ENVIOS_POR_CLIENTE", "ENVIOS_DEMORADOS",
                   "INCIDENTES_ABIERTOS", "TIEMPOS_POR_CLIENTE", "PROMEDIO_INCIDENTES",
                   "TIEMPOS_ENTREGA", "COSTOS_TRANSPORTE", "RANKING_RUTAS", "RANKING_CLIENTES",
                   "DESEMPENO_HISTORICO", "RIESGO_DEMORA"] },
    parametros:       { bsonType: "object" },     // fechas, país, cliente... según el tipo
    formato:          { enum: ["PANTALLA", "CSV", "JSON"] },
    estado:           { enum: ["SOLICITADO", "EN_PROCESO", "GENERADO", "ERROR"] },
    fecha_solicitud:  { bsonType: "date" },
    fecha_generacion: { bsonType: "date" },
    resultado:        { bsonType: "array", items: { bsonType: "object" } },
    error:            { bsonType: "string" },
    // fase 9: reportes facturables (se cobran en la factura del cliente)
    id_cliente:       ENTERO,
    facturable:       { bsonType: "bool" },
    id_factura:       ENTERO,
    numero_factura:   { bsonType: "string" }
  }
});
db.reportes.createIndex({ id_usuario: 1, fecha_solicitud: -1 }, { name: "ix_usuario_fecha" });
db.reportes.createIndex({ estado: 1, fecha_solicitud: 1 },      { name: "ix_cola" });

// ---------------------------------------------------------------------
// 8. predicciones_riesgo — resultado del proceso diario
// ---------------------------------------------------------------------
crear("predicciones_riesgo", {
  bsonType: "object",
  required: ["id_envio", "fecha_calculo", "probabilidad_demora", "riesgo_climatico",
             "riesgo_operativo", "nivel", "modelo"],
  properties: {
    id_envio:            ENTERO,
    fecha_calculo:       { bsonType: "date" },
    probabilidad_demora: { bsonType: ["double", "decimal"], minimum: 0, maximum: 1 },
    riesgo_climatico:    { bsonType: ["double", "decimal"], minimum: 0, maximum: 1 },
    riesgo_operativo:    { bsonType: ["double", "decimal"], minimum: 0, maximum: 1 },
    nivel:               { enum: ["BAJO", "MEDIO", "ALTO", "CRITICO"] },
    modelo: {
      bsonType: "object",
      required: ["nombre", "version"],
      properties: { nombre: { bsonType: "string" }, version: { bsonType: "string" } }
    },
    variables: { bsonType: "object" }              // insumos usados en el cálculo
  }
});
db.predicciones_riesgo.createIndex({ id_envio: 1, fecha_calculo: -1 },            { name: "ix_envio_fecha" });
db.predicciones_riesgo.createIndex({ fecha_calculo: -1, probabilidad_demora: -1 }, { name: "ix_fecha_prob" });

// ---------------------------------------------------------------------
// Particionamiento (solo en un clúster con sharding; en el TP corre un
// único nodo, por eso queda comentado).
// ---------------------------------------------------------------------
// sh.enableSharding("logistica_docs");
// sh.shardCollection("logistica_docs.eventos_logisticos", { id_envio: "hashed" });
// sh.shardCollection("logistica_docs.incidentes",         { id_envio: "hashed" });
// sh.shardCollection("logistica_docs.mensajes",           { id_conversacion: 1, fecha_hora: 1 });

print("logistica_docs: " + db.getCollectionNames().length + " colecciones creadas");
