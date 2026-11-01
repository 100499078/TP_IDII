"""Entidades del dominio.

Son clases simples (dataclasses) que representan los conceptos del negocio.
No saben nada de SQL, documentos ni grafos: los repositorios traducen entre
estas clases y cada base de datos.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass(frozen=True)
class Rol:
    id_rol: int
    nombre: str
    nivel_permisos: int          # 1 = consulta ... 5 = administrador
    descripcion: str | None = None


@dataclass
class Usuario:
    id_usuario: int
    nombre: str
    apellido: str
    email: str
    estado: str                  # ACTIVO | BLOQUEADO | BAJA
    id_actor: int | None = None  # None = usuario interno de la empresa
    roles: list[Rol] = field(default_factory=list)
    fecha_registro: datetime | None = None

    @property
    def nombre_completo(self) -> str:
        return f"{self.nombre} {self.apellido}"

    @property
    def nivel_permisos(self) -> int:
        """El nivel efectivo es el más alto entre sus roles."""
        return max((r.nivel_permisos for r in self.roles), default=0)


@dataclass
class Sesion:
    id_sesion: str
    id_usuario: int
    email: str
    nombre: str
    roles: list[str]
    nivel_permisos: int
    fecha_inicio: datetime
    ultimo_acceso: datetime
    id_actor: int | None = None      # si el usuario pertenece a un cliente, solo ve lo suyo


# =====================================================================
# Fase 2 — gestión operativa
# =====================================================================

TIPOS_ACTOR = ("CLIENTE", "OPERADOR", "PROVEEDOR")
MEDIOS = ("TERRESTRE", "MARITIMO", "AEREO")
PRIORIDADES = ("BAJA", "MEDIA", "ALTA", "URGENTE")
TIPOS_CONTENEDOR = ("DRY", "REEFER", "OPEN_TOP", "TANK", "FLAT_RACK")
TIPOS_SENSOR = ("TEMPERATURA", "HUMEDAD", "VIBRACION", "GPS", "MULTI")
TIPOS_VEHICULO = ("CAMION", "TREN", "BUQUE", "AVION")
TIPOS_UBICACION = ("PUERTO", "AEROPUERTO", "CENTRO_DIST", "CIUDAD")

# Qué medio de transporte puede hacer cada tipo de vehículo
MEDIO_DE_VEHICULO = {"CAMION": "TERRESTRE", "TREN": "TERRESTRE", "BUQUE": "MARITIMO", "AVION": "AEREO"}

# Máquina de estados del envío: desde cada estado, a cuáles se puede pasar
TRANSICIONES_ENVIO: dict[str, tuple[str, ...]] = {
    "CREADO": ("EN_TRANSITO", "CANCELADO"),
    "EN_TRANSITO": ("EN_ADUANA", "DEMORADO", "ENTREGADO"),
    "EN_ADUANA": ("EN_TRANSITO", "DEMORADO", "ENTREGADO"),
    "DEMORADO": ("EN_TRANSITO", "EN_ADUANA", "ENTREGADO"),
    "ENTREGADO": (),
    "CANCELADO": (),
}
ESTADOS_ENVIO_EN_CURSO = ("CREADO", "EN_TRANSITO", "EN_ADUANA", "DEMORADO")


@dataclass
class Actor:
    """Cliente, operador logístico o proveedor (supertipo `actor` + su subtipo)."""
    id_actor: int | None
    tipo: str
    razon_social: str
    cuit: str
    email: str
    pais: str
    estado: str = "ACTIVO"
    telefono: str | None = None
    direccion: str | None = None
    # datos propios de cada subtipo
    limite_credito: float | None = None         # CLIENTE
    condicion_pago_dias: int | None = None      # CLIENTE
    medios: list[str] = field(default_factory=list)   # OPERADOR
    rubro: str | None = None                    # PROVEEDOR


@dataclass
class Ubicacion:
    id_ubicacion: int | None
    codigo: str            # UN/LOCODE, ej. ARBUE
    nombre: str
    tipo: str
    pais: str
    latitud: float
    longitud: float


@dataclass
class Tarifa:
    id_tarifa: int | None
    medio_transporte: str
    precio_base: float
    precio_kg: float
    vigente_desde: date
    vigente_hasta: date | None = None

    def costo(self, peso_kg: float) -> float:
        return round(float(self.precio_base) + float(self.precio_kg) * float(peso_kg), 2)


@dataclass
class Vehiculo:
    id_vehiculo: int | None
    tipo: str
    identificacion: str
    id_operador: int
    estado: str = "OPERATIVO"
    operador: str | None = None      # razón social, solo para mostrar


@dataclass
class Contenedor:
    id_contenedor: int | None
    codigo_internacional: str
    tipo: str
    capacidad_m3: float
    estado: str = "DISPONIBLE"
    sensores: int = 0                # cantidad de sensores instalados, solo para mostrar


@dataclass
class Sensor:
    id_sensor: int | None
    tipo: str
    fabricante: str
    modelo: str | None = None
    estado: str = "ACTIVO"
    id_contenedor: int | None = None
    fecha_instalacion: date | None = None
    temp_min: float | None = None
    temp_max: float | None = None
    codigo_contenedor: str | None = None   # solo para mostrar


@dataclass
class Tramo:
    id_tramo: int | None
    id_envio: int
    orden: int
    medio_transporte: str
    origen: str            # código de ubicación
    destino: str
    id_operador: int
    fecha_salida: datetime
    fecha_llegada_estimada: datetime
    id_vehiculo: int | None = None
    fecha_llegada_real: datetime | None = None
    costo: float = 0.0
    operador: str | None = None      # solo para mostrar
    vehiculo: str | None = None      # solo para mostrar


@dataclass
class Envio:
    id_envio: int | None
    id_cliente: int
    origen: str            # código de ubicación
    destino: str
    fecha_entrega_estimada: datetime
    peso_kg: float
    volumen_m3: float
    prioridad: str = "MEDIA"
    estado: str = "CREADO"
    fecha_creacion: datetime | None = None
    fecha_entrega_real: datetime | None = None
    cliente: str | None = None                       # razón social, solo para mostrar
    contenedores: list[int] = field(default_factory=list)
    tramos: list[Tramo] = field(default_factory=list)

    @property
    def en_curso(self) -> bool:
        return self.estado in ESTADOS_ENVIO_EN_CURSO

    @property
    def costo_total(self) -> float:
        return round(sum(t.costo for t in self.tramos), 2)

    @property
    def ultimo_destino(self) -> str:
        """Dónde termina la ruta cargada hasta ahora (el origen si todavía no hay tramos)."""
        return self.tramos[-1].destino if self.tramos else self.origen


# =====================================================================
# Fase 3 — telemetría IoT
# =====================================================================

TIPOS_ALERTA = ("TEMPERATURA_ALTA", "TEMPERATURA_BAJA", "HUMEDAD", "VIBRACION", "BATERIA_BAJA",
                "SENSOR_INACTIVO", "RETRASO", "OTRO")
@dataclass
class ReglaAlerta:
    """Fase 9: umbral configurable de una alerta (tabla regla_alerta de PostgreSQL)."""
    tipo: str
    umbral: float | None
    unidad: str | None
    activa: bool
    descripcion: str
    fecha_modificacion: datetime | None = None


ESTADOS_ALERTA = ("NUEVA", "VISTA", "RESUELTA")


@dataclass
class Lectura:
    """Una medición de un sensor. Según el tipo de sensor, algunos valores quedan en None."""
    id_sensor: int
    id_contenedor: int
    fecha_hora: datetime                 # siempre en UTC
    temperatura: float | None = None     # °C
    humedad: float | None = None         # % relativa
    vibracion: float | None = None       # g (aceleración)
    latitud: float | None = None
    longitud: float | None = None
    bateria: int | None = None           # %
    id_envio: int | None = None          # envío en curso del contenedor, si lo hay

    @property
    def dia(self) -> date:
        """Día UTC de la lectura: es parte de la clave de partición en Cassandra."""
        return self.fecha_hora.date()

    @property
    def tiene_posicion(self) -> bool:
        return self.latitud is not None and self.longitud is not None


@dataclass
class Alerta:
    tipo: str
    fecha: datetime
    id_sensor: int
    id_contenedor: int
    mensaje: str
    valor: float | None = None
    umbral: float | None = None
    id_envio: int | None = None
    estado: str = "NUEVA"
    id_alerta: str | None = None         # ObjectId de MongoDB como texto


# =====================================================================
# Fase 4 — trazabilidad e incidentes
# =====================================================================

TIPOS_EVENTO = ("CARGA_INICIADA", "EN_TRANSITO", "ADUANA", "RETRASO", "DESCARGA", "ENTREGADO", "OTRO")
TIPOS_INCIDENTE = ("TEMPERATURA_FUERA_DE_RANGO", "DANIO_CARGA", "DEMORA_ADUANA", "ROBO", "CLIMA",
                   "FALLA_VEHICULO", "DOCUMENTACION", "OTRO")
TRANSICIONES_INCIDENTE: dict[str, tuple[str, ...]] = {
    "ABIERTO": ("EN_ANALISIS", "RESUELTO"),
    "EN_ANALISIS": ("RESUELTO",),
    "RESUELTO": ("CERRADO", "EN_ANALISIS"),      # se puede reabrir si la solución no alcanzó
    "CERRADO": (),
}
ESTADOS_INCIDENTE_ABIERTOS = ("ABIERTO", "EN_ANALISIS")


@dataclass
class EventoLogistico:
    """Un hito en la vida de un envío (documento de MongoDB, colección eventos_logisticos)."""
    id_envio: int
    tipo: str
    fecha_hora: datetime
    codigo_ubicacion: str
    pais: str
    latitud: float
    longitud: float
    nombre_ubicacion: str | None = None
    descripcion: str | None = None
    detalle: dict = field(default_factory=dict)
    id_usuario: int | None = None           # None = lo generó el sistema
    nombre_usuario: str | None = None
    id_evento: str | None = None            # ObjectId como texto


@dataclass
class Incidente:
    """Un problema durante un envío (documento de MongoDB, colección incidentes)."""
    id_envio: int
    tipo: str
    severidad: int                          # 1 (leve) a 5 (grave)
    fecha: datetime
    id_cliente: int
    pais_origen: str
    pais_destino: str
    ruta: str                               # "ARROS-NLRTM" (origen-destino del envío)
    descripcion: str = ""
    id_tramo: int | None = None
    medio: str | None = None
    estado: str = "ABIERTO"
    historial: list[dict] = field(default_factory=list)
    id_incidente: str | None = None         # ObjectId como texto


# =====================================================================
# Fase 5 — mensajería
# =====================================================================

@dataclass
class Integrante:
    id_usuario: int
    nombre: str
    rol_en_grupo: str                    # ADMIN | MIEMBRO
    desde: datetime


@dataclass
class GrupoTrabajo:
    nombre: str
    integrantes: list[Integrante]
    creado_por: int
    fecha_creacion: datetime
    descripcion: str = ""
    activo: bool = True
    id_grupo: str | None = None          # ObjectId como texto

    def es_admin(self, id_usuario: int) -> bool:
        return any(i.id_usuario == id_usuario and i.rol_en_grupo == "ADMIN" for i in self.integrantes)

    def es_integrante(self, id_usuario: int) -> bool:
        return any(i.id_usuario == id_usuario for i in self.integrantes)


@dataclass
class Conversacion:
    tipo: str                            # PRIVADA | GRUPAL
    participantes: list[int]
    creada: datetime
    id_grupo: str | None = None
    ultimo_resumen: str | None = None
    ultimo_fecha: datetime | None = None
    ultimo_id_usuario: int | None = None
    id_conversacion: str | None = None
    titulo: str = ""                     # solo para mostrar (nombre del grupo o del otro participante)


@dataclass
class Mensaje:
    id_conversacion: str
    id_remitente: int
    nombre_remitente: str
    fecha_hora: datetime
    contenido: str
    tipo: str = "TEXTO"                  # TEXTO | ARCHIVO | SISTEMA
    leido_por: list[int] = field(default_factory=list)
    id_mensaje: str | None = None


# =====================================================================
# Fase 6 — comercial (facturas, pagos, cuenta corriente)
# =====================================================================
from decimal import ROUND_HALF_UP, Decimal  # noqa: E402

IVA = Decimal("0.21")
PRECIO_REPORTE = Decimal("15000.00")    # fase 9: lo que se le cobra a un cliente por cada reporte (más IVA)
MEDIOS_PAGO = ("TRANSFERENCIA", "TARJETA", "CHEQUE", "EFECTIVO")
ESTADOS_FACTURA = ("PENDIENTE", "PARCIAL", "PAGADA", "VENCIDA", "ANULADA")
ESTADOS_FACTURA_ABIERTA = ("PENDIENTE", "PARCIAL", "VENCIDA")


def pesos(valor) -> Decimal:
    """Dinero siempre como Decimal con 2 decimales (float acumula errores: 0.1 + 0.2 != 0.3)."""
    return Decimal(str(valor)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@dataclass
class ItemFactura:
    descripcion: str
    cantidad: Decimal
    precio_unitario: Decimal
    id_envio: int | None = None
    id_item: int | None = None
    id_reporte: str | None = None       # fase 9: reporte de MongoDB que se cobra en este ítem

    @property
    def subtotal(self) -> Decimal:
        return pesos(self.cantidad * self.precio_unitario)


@dataclass
class PagoFactura:
    id_pago: int
    fecha: datetime
    monto: Decimal
    medio_pago: str


@dataclass
class Factura:
    id_actor: int
    tipo: str                         # EMITIDA (a un cliente) | RECIBIDA (de un operador o proveedor)
    numero: str
    fecha_emision: date
    fecha_vencimiento: date
    importe_total: Decimal
    estado: str = "PENDIENTE"
    items: list[ItemFactura] = field(default_factory=list)
    pagos: list[PagoFactura] = field(default_factory=list)
    id_factura: int | None = None
    razon_social: str | None = None   # solo para mostrar

    @property
    def pagado(self) -> Decimal:
        return pesos(sum((p.monto for p in self.pagos), Decimal("0")))

    @property
    def pendiente(self) -> Decimal:
        return pesos(self.importe_total - self.pagado)
