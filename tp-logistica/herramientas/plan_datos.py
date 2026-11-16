"""El PLAN de los datos de prueba (fase 8): funciones puras, sin bases de datos.

Con una semilla fija decide qué pasa en unos meses de operación: qué envíos se crean, por
qué ruta, cuándo sale y llega cada tramo, cuáles se demoran, qué incidentes hay, cuándo se
factura y se cierra cada mes. El resultado es una lista de EVENTOS con fecha y hora; quien
los ejecuta (herramientas/datos_prueba.py) llama a los servicios en ese orden.

Que el plan sea puro (sin bases) permite probarlo con pytest: tests/test_datos_prueba.py
comprueba que respeta las reglas de la aplicación (ruta continua, máquina de estados,
un contenedor no viaja en dos envíos a la vez, etc.).
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.dominio.modelos import TRANSICIONES_ENVIO
from app.dominio.validaciones import calcular_digito_cuit, validar_codigo_contenedor

H = timedelta(hours=1)

# ---------------------------------------------------------------------------------- catálogos
# (código UN/LOCODE, nombre, tipo, latitud, longitud): se suman a las 10 de inicializar_bases
UBICACIONES_EXTRA = [
    ("ARCOR", "Centro de distribución Córdoba", "CENTRO_DIST", -31.4201, -64.1888),
    ("DEHAM", "Puerto de Hamburgo", "PUERTO", 53.5461, 9.9661),
    ("ESMAD", "Aeropuerto de Madrid-Barajas", "AEROPUERTO", 40.4983, -3.5676),
    ("PECLL", "Puerto del Callao", "PUERTO", -12.0464, -77.1425),
    ("BRGRU", "Aeropuerto de Guarulhos", "AEROPUERTO", -23.4356, -46.4731),
]
PAIS = lambda codigo: codigo[:2]  # noqa: E731   (el país sale del código UN/LOCODE)

# Operadores: clave -> (razón social, 8 dígitos del CUIT, email, país, medios, vehículos (tipo, identificación))
# Los dos primeros son los de la demo (se buscan por CUIT, no se crean de nuevo).
OPERADORES = {
    "andinos": ("Transportes Andinos S.R.L.", "30-71333333-2", "trafico@tandinos.com", "AR", ["TERRESTRE"],
                [("CAMION", "AB123CD")]),
    "atlantica": ("Naviera Atlántica S.A.", "30-71444444-8", "operaciones@navatlantica.com", "UY",
                  ["MARITIMO", "TERRESTRE"], [("BUQUE", "IMO9321483"), ("CAMION", "AE456FG")]),
    "aerocargo": ("Aerocargo del Plata S.A.", 71600010, "ops@aerocargoplata.com", "AR", ["AEREO"],
                  [("AVION", "LV-FQZ"), ("AVION", "LV-GKP")]),
    "pacifico": ("Pacific Ocean Lines S.A.", 71600020, "booking@pacificlines.cl", "CL", ["MARITIMO"],
                 [("BUQUE", "IMO9454436"), ("BUQUE", "IMO9708849")]),
    "ferrocentral": ("Ferrocarril Central Cargas S.A.", 71600030, "cargas@ferrocentral.com.ar", "AR",
                     ["TERRESTRE"], [("TREN", "FCC-0451"), ("CAMION", "AF789GH")]),
    "mercosur": ("Logística Mercosur S.R.L.", 71600040, "despacho@logmercosur.com", "AR", ["TERRESTRE"],
                 [("CAMION", "AG321JK"), ("CAMION", "AG654LM")]),
}
DEMO = {"andinos", "atlantica"}

# Rutas: clave -> (origen, [(medio, destino, horas)], probabilidad de demora, incidentes promedio, tipos)
RUTAS = {
    "ROS-RTM": ("ARROS", [("TERRESTRE", "ARBUE", 8), ("MARITIMO", "NLRTM", 22 * 24)], 0.25, 0.5,
                ["CLIMA", "DEMORA_ADUANA", "DANIO_CARGA", "DOCUMENTACION"]),
    "BUE-RTM": ("ARBUE", [("MARITIMO", "NLRTM", 21 * 24)], 0.2, 0.4, ["CLIMA", "DEMORA_ADUANA", "DANIO_CARGA"]),
    "BUE-HAM": ("ARBUE", [("MARITIMO", "DEHAM", 23 * 24)], 0.2, 0.4, ["CLIMA", "DEMORA_ADUANA", "DOCUMENTACION"]),
    "BUE-VLC": ("ARBUE", [("MARITIMO", "ESVLC", 18 * 24)], 0.15, 0.3, ["CLIMA", "DANIO_CARGA"]),
    "BUE-SSZ": ("ARBUE", [("MARITIMO", "BRSSZ", 4 * 24)], 0.45, 0.9,
                ["DEMORA_ADUANA", "DEMORA_ADUANA", "DOCUMENTACION", "FALLA_VEHICULO"]),
    "ROS-SSZ": ("ARROS", [("TERRESTRE", "ARBUE", 8), ("MARITIMO", "BRSSZ", 4 * 24)], 0.45, 0.9,
                ["DEMORA_ADUANA", "DEMORA_ADUANA", "DOCUMENTACION"]),
    "COR-SSZ": ("ARCOR", [("TERRESTRE", "ARROS", 6), ("TERRESTRE", "ARBUE", 8), ("MARITIMO", "BRSSZ", 4 * 24)],
                0.4, 0.8, ["DEMORA_ADUANA", "FALLA_VEHICULO", "DOCUMENTACION"]),
    "BUE-MVD": ("ARBUE", [("MARITIMO", "UYMVD", 14)], 0.1, 0.2, ["DOCUMENTACION", "CLIMA"]),
    "BUE-VAP": ("ARBUE", [("TERRESTRE", "CLVAP", 60)], 0.4, 1.0, ["CLIMA", "CLIMA", "CLIMA", "FALLA_VEHICULO", "ROBO"]),
    "BUE-CLL": ("ARBUE", [("MARITIMO", "PECLL", 12 * 24)], 0.2, 0.4, ["CLIMA", "DEMORA_ADUANA"]),
    "SHA-ROS": ("CNSHA", [("MARITIMO", "ARBUE", 35 * 24), ("TERRESTRE", "ARROS", 8)], 0.35, 0.7,
                ["DEMORA_ADUANA", "CLIMA", "DANIO_CARGA", "DOCUMENTACION"]),
    "EZE-MIA": ("AREZE", [("AEREO", "USMIA", 11)], 0.1, 0.2, ["DOCUMENTACION", "DANIO_CARGA"]),
    "EZE-MAD": ("AREZE", [("AEREO", "ESMAD", 13)], 0.1, 0.2, ["DOCUMENTACION", "DANIO_CARGA"]),
    "ROS-MIA": ("ARROS", [("TERRESTRE", "AREZE", 5), ("AEREO", "USMIA", 11)], 0.15, 0.3, ["DOCUMENTACION", "ROBO"]),
    "EZE-GRU": ("AREZE", [("AEREO", "BRGRU", 3)], 0.1, 0.2, ["DOCUMENTACION"]),
}

# Clientes: clave -> (razón social, CUIT o 8 dígitos, email, país, límite de crédito, días de pago,
#                     tipo de contenedor, rutas, (kg mín, kg máx), cómo paga, cuántos envíos (peso relativo))
CLIENTES = {
    "vinos": ("Vinos del Sur S.A.", "30-71111111-1", "compras@vinosdelsur.com", "AR", 5_000_000, 30,
              "REEFER", ["ROS-RTM", "BUE-VLC", "BUE-HAM"], (8_000, 20_000), "PUNTUAL", 3),
    "agro": ("Agro Exportadora Pampa S.A.", "30-71222222-7", "logistica@agropampa.com", "AR", 2_000_000, 60,
             "DRY", ["BUE-SSZ", "BUE-RTM", "BUE-MVD"], (15_000, 26_000), "NORMAL", 3),
    "frigorifico": ("Frigorífico Pampeano S.A.", 71700010, "compras@frigorificopampeano.com", "AR", 8_000_000, 30,
                    "REEFER_CONGELADO", ["ROS-RTM", "BUE-HAM", "BUE-VLC"], (18_000, 24_000), "PUNTUAL", 3),
    "citricos": ("Citrícola Tucumana S.A.", 71700020, "exportaciones@citricolatucumana.com", "AR", 4_000_000, 45,
                 "REEFER", ["BUE-RTM", "BUE-VLC", "BUE-HAM"], (16_000, 22_000), "NORMAL", 2),
    "biofarma": ("Laboratorios Biofarma S.A.", 71700030, "comex@biofarma.com", "AR", 3_000_000, 30,
                 "REEFER", ["EZE-MIA", "EZE-MAD", "EZE-GRU"], (400, 2_500), "PUNTUAL", 2),
    "electro": ("Electro Importaciones S.A.", 71700040, "logistica@electroimport.com", "AR", 1_500_000, 30,
                "DRY", ["SHA-ROS"], (8_000, 20_000), "MOROSO", 2),
    "maderas": ("Maderas del Litoral S.R.L.", 71700050, "ventas@maderaslitoral.com", "AR", 1_000_000, 60,
                "OPEN_TOP", ["ROS-SSZ", "BUE-MVD"], (10_000, 24_000), "NORMAL", 1),
    "quimica": ("Química Austral S.A.", 71700060, "despachos@quimicaaustral.com", "AR", 2_500_000, 30,
                "TANK", ["BUE-SSZ", "BUE-CLL"], (12_000, 20_000), "NORMAL", 1),
    "textil": ("Textil Andina S.A.", 71700070, "abastecimiento@textilandina.com", "AR", 800_000, 60,
               "DRY", ["BUE-VAP", "COR-SSZ"], (4_000, 12_000), "MOROSO", 2),
    "autopartes": ("Autopartes Córdoba S.A.", 71700080, "logistica@autopartescba.com", "AR", 2_000_000, 30,
                   "DRY", ["COR-SSZ", "EZE-GRU", "BUE-VAP"], (2_000, 16_000), "PUNTUAL", 2),
}

# Contenedores propios de la carga: (tipo, capacidad m³, cantidad, sensores por contenedor, rango de temperatura)
CONTENEDORES = [
    ("REEFER", 67.7, 6, ["TEMPERATURA", "GPS"], (2.0, 8.0)),
    ("REEFER", 33.2, 2, ["TEMPERATURA"], (2.0, 8.0)),
    ("REEFER_CONGELADO", 67.7, 4, ["TEMPERATURA", "GPS"], (-25.0, -15.0)),
    ("DRY", 67.7, 8, ["GPS"], None),
    ("DRY", 33.2, 4, [], None),
    ("TANK", 24.0, 3, ["GPS"], None),
    ("OPEN_TOP", 32.6, 3, [], None),
]

# Usuarios de la carga: clave -> (nombre, apellido, email, rol, cliente o None)
USUARIOS = {
    "martin": ("Martín", "Ríos", "trafico@logistica.local", "OPERADOR", None),
    "paula": ("Paula", "Méndez", "supervision@logistica.local", "SUPERVISOR", None),
    "diego": ("Diego", "Sosa", "analisis@logistica.local", "ANALISTA", None),
    "frigorifico": ("Rosa", "Benítez", "rbenitez@frigorificopampeano.com", "CLIENTE", "frigorifico"),
    "electro": ("Hernán", "Paz", "hpaz@electroimport.com", "CLIENTE", "electro"),
    "biofarma": ("Lucía", "Ferro", "lferro@biofarma.com", "CLIENTE", "biofarma"),
}
USUARIOS_DEMO = {"olga": "operador@logistica.local", "carlos": "cliente@vinosdelsur.com"}
USUARIO_DE_CLIENTE = {"vinos": "carlos", "frigorifico": "frigorifico", "electro": "electro", "biofarma": "biofarma"}

ENVIOS_POR_MES = 13.5
LECTURAS_OBJETIVO = 10_000


# ---------------------------------------------------------------------------------- utilidades
def cuit(valor: str | int) -> str:
    """El CUIT tal cual, o uno válido armado a partir de 8 dígitos (busca el dígito verificador)."""
    if isinstance(valor, str):
        return valor
    while True:
        diez = f"30{valor}"
        digito = calcular_digito_cuit(diez)
        if digito is not None:
            return f"30-{valor}-{digito}"
        valor += 1


def codigo_contenedor(numero: int, duenio: str = "LPRU") -> str:
    """Código ISO 6346 válido: dueño + categoría U + 6 dígitos + dígito de control."""
    base = f"{duenio}{numero:06d}"
    for control in range(10):
        if validar_codigo_contenedor(base + str(control)) is None:
            return base + str(control)
    raise ValueError("sin dígito de control")


def _entre(rng: random.Random, a: float, b: float) -> float:
    return rng.uniform(a, b)


# ---------------------------------------------------------------------------------- modelo del plan
@dataclass
class Evento:
    momento: datetime
    tipo: str          # CREAR, CANCELAR, ESTADO, LLEGADA, INCIDENTE, INCIDENTE_ESTADO, LECTURAS, FACTURAR,
    datos: dict        # VENCIDAS, CIERRE, RECIBIDAS, GRUPO, MENSAJE_GRUPO, MENSAJE_PRIVADO
    orden: int = 0     # desempate entre eventos de la misma hora


@dataclass
class TramoPlan:
    medio: str
    origen: str
    destino: str
    operador: str
    vehiculo: str
    salida: datetime
    llegada_estimada: datetime
    llegada_real: datetime | None = None
    aduana: bool = False
    horas_aduana: float = 0.0


@dataclass
class EnvioPlan:
    n: int
    cliente: str
    ruta: str
    origen: str
    destino: str
    creacion: datetime
    entrega_estimada: datetime
    peso_kg: float
    volumen_m3: float
    prioridad: str
    tipo_contenedor: str
    contenedores: list[int] = field(default_factory=list)       # índices en Plan.contenedores
    tramos: list[TramoPlan] = field(default_factory=list)
    cancelado: datetime | None = None
    eventos: list[Evento] = field(default_factory=list)
    liberacion: datetime | None = None                          # cuándo quedan libres los contenedores


@dataclass
class ContenedorPlan:
    codigo: str
    tipo: str              # tipo de la base (REEFER_CONGELADO se guarda como REEFER)
    clase: str             # para elegir: REEFER, REEFER_CONGELADO, DRY...
    capacidad: float
    sensores: list[str]
    rango: tuple[float, float] | None


@dataclass
class Plan:
    inicio: datetime
    ahora: datetime
    contenedores: list[ContenedorPlan]
    envios: list[EnvioPlan]
    eventos: list[Evento]                  # TODOS, ordenados (los de los envíos y los generales)
    paso_lecturas_h: int

    def eventos_hasta(self, limite: datetime) -> list[Evento]:
        return [e for e in self.eventos if e.momento <= limite]


# ---------------------------------------------------------------------------------- armado
def armar_contenedores() -> list[ContenedorPlan]:
    lista, numero = [], 100
    for clase, capacidad, cantidad, sensores, rango in CONTENEDORES:
        for _ in range(cantidad):
            numero += 1
            tipo = "REEFER" if clase.startswith("REEFER") else clase
            lista.append(ContenedorPlan(codigo_contenedor(numero), tipo, clase, capacidad, sensores, rango))
    return lista


def _operador_y_vehiculo(rng: random.Random, medio: str, ruta: str) -> tuple[str, str]:
    if medio == "MARITIMO":
        candidatos = ["pacifico"] if ruta in ("BUE-CLL", "SHA-ROS") else ["atlantica", "atlantica", "pacifico"]
    elif medio == "AEREO":
        candidatos = ["aerocargo"]
    else:
        candidatos = ["andinos"] if ruta == "BUE-VAP" else ["andinos", "ferrocentral", "mercosur", "atlantica"]
    operador = rng.choice(candidatos)
    from app.dominio.modelos import MEDIO_DE_VEHICULO
    vehiculos = [ident for tipo, ident in OPERADORES[operador][5] if MEDIO_DE_VEHICULO[tipo] == medio]
    return operador, rng.choice(vehiculos)


def _tramos(rng: random.Random, ruta: str, primera_salida: datetime) -> list[TramoPlan]:
    origen, pasos, *_ = RUTAS[ruta]
    tramos, salida, desde = [], primera_salida, origen
    for i, (medio, destino, horas) in enumerate(pasos):
        operador, vehiculo = _operador_y_vehiculo(rng, medio, ruta)
        llegada = salida + H * horas * _entre(rng, 0.9, 1.1)
        aduana = medio in ("MARITIMO", "AEREO") and PAIS(desde) != PAIS(destino)
        horas_aduana = (_entre(rng, 8, 36) if medio == "MARITIMO" else _entre(rng, 3, 10)) if aduana else 0.0
        tramos.append(TramoPlan(medio, desde, destino, operador, vehiculo, salida, llegada, aduana=aduana,
                                horas_aduana=horas_aduana))
        # el tramo siguiente sale después de la llegada estimada (y de la aduana, si hay)
        espera = horas_aduana + _entre(rng, 6, 24) if aduana else _entre(rng, 4, 30)
        salida, desde = llegada + H * espera, destino
    return tramos


def _estados(e: EnvioPlan, rng: random.Random) -> list[tuple[datetime, str]]:
    """La secuencia de cambios de estado de un envío que viaja (sin cancelar)."""
    cambios = [(e.tramos[0].salida, "EN_TRANSITO")]
    for i, t in enumerate(e.tramos):
        ultimo = i == len(e.tramos) - 1
        if ultimo and (t.llegada_real - t.llegada_estimada) > 24 * H:
            cambios.append((t.llegada_estimada + 12 * H, "DEMORADO"))          # ya se sabe que llega tarde
        if t.aduana:
            cambios.append((t.llegada_real + H / 2, "EN_ADUANA"))
            fin = t.llegada_real + H * t.horas_aduana
            cambios.append((fin, "ENTREGADO" if ultimo else "EN_TRANSITO"))
        elif ultimo:
            cambios.append((t.llegada_real + H * _entre(rng, 1, 6), "ENTREGADO"))
    return cambios


def validar_estados(cambios: list[tuple[datetime, str]]) -> None:
    """Falla si la secuencia no respeta la máquina de estados del envío (la usan las pruebas)."""
    actual, momento = "CREADO", None
    for cuando, nuevo in cambios:
        if nuevo not in TRANSICIONES_ENVIO[actual]:
            raise ValueError(f"{actual} -> {nuevo} no está permitido")
        if momento is not None and cuando < momento:
            raise ValueError("los cambios de estado no están en orden")
        actual, momento = nuevo, cuando


def _incidentes(rng: random.Random, e: EnvioPlan, tipos: list[str], promedio: float) -> list[dict]:
    cantidad = sum(1 for _ in range(4) if rng.random() < promedio * 1.2 / 4)
    if e.tipo_contenedor.startswith("REEFER") and rng.random() < 0.15:
        cantidad += 1
        tipos = tipos + ["TEMPERATURA_FUERA_DE_RANGO"] * 4
    desde = e.tramos[0].salida + H
    hasta = e.tramos[-1].llegada_real - H
    resultado = []
    for _ in range(cantidad):
        if hasta <= desde:
            break
        momento = desde + (hasta - desde) * rng.random()
        indice = max(i for i, t in enumerate(e.tramos) if t.salida <= momento)
        tipo = rng.choice(tipos)
        severidad = rng.choices([1, 2, 3, 4, 5], weights=[25, 30, 25, 15, 5])[0]
        resultado.append({"momento": momento, "tramo": indice, "tipo": tipo, "severidad": severidad})
    return resultado


def armar_plan(ahora: datetime, meses: int = 6, semilla: int = 2026) -> Plan:
    rng = random.Random(semilla)
    ahora = ahora.replace(second=0, microsecond=0)
    inicio = (ahora - timedelta(days=30 * meses)).replace(hour=0, minute=0)
    contenedores = armar_contenedores()
    libre_desde = [inicio] * len(contenedores)              # cuándo queda libre cada contenedor

    cantidad = round(ENVIOS_POR_MES * meses)
    span = (ahora - 12 * H) - inicio
    creaciones = [inicio + span * rng.random() for _ in range(cantidad)]
    # unos cuantos más en las últimas 3 semanas: al "hoy" de la carga quedan envíos en curso y recién creados
    recientes = timedelta(days=21)
    creaciones += [ahora - recientes + (recientes - 6 * H) * rng.random() for _ in range(2 * meses)]
    creaciones.append(ahora - H * _entre(rng, 6, 30))           # uno recién creado, que todavía no salió
    creaciones.sort()
    claves = list(CLIENTES)
    pesos_cliente = [CLIENTES[c][10] for c in claves]
    envios: list[EnvioPlan] = []
    forzados: set[str] = set()

    for creacion in creaciones:
        creacion = creacion.replace(second=0, microsecond=0)
        cliente = rng.choices(claves, weights=pesos_cliente)[0]
        _, _, _, _, _, _, clase, rutas, (kg_min, kg_max), _, _ = CLIENTES[cliente]
        ruta = rng.choice(rutas)
        peso = round(_entre(rng, kg_min, kg_max), -1)
        n_cont = 2 if peso > 20_000 and clase in ("DRY", "REEFER", "REEFER_CONGELADO") else 1
        prioridad = rng.choices(["BAJA", "MEDIA", "ALTA", "URGENTE"], weights=[15, 50, 28, 7])[0]
        origen, pasos, prob_demora, inc_promedio, tipos_inc = RUTAS[ruta]

        # contenedores libres de esa clase en el momento de la creación
        libres = [i for i, c in enumerate(contenedores) if c.clase == clase and libre_desde[i] < creacion]
        if len(libres) < n_cont:
            n_cont = 1
        if not libres:
            continue                                         # no hay contenedor: ese envío no se hace
        elegidos = sorted(libres, key=lambda i: (libre_desde[i], i))[:n_cont]
        capacidad = sum(contenedores[i].capacidad for i in elegidos)
        volumen = round(capacidad * _entre(rng, 0.55, 0.95), 1)

        primera_salida = creacion + H * _entre(rng, 18, 72)
        if creacion > ahora - 48 * H:                           # los de las últimas 48 h salen después de hoy
            primera_salida = max(primera_salida, ahora + H * _entre(rng, 6, 30))
        tramos = _tramos(rng, ruta, primera_salida)
        ultimo = tramos[-1]
        entrega = ultimo.llegada_estimada + H * (ultimo.horas_aduana + _entre(rng, 24, 72))
        e = EnvioPlan(len(envios) + 1, cliente, ruta, origen, pasos[-1][1], creacion, entrega.replace(second=0,
                      microsecond=0), peso, volumen, prioridad, clase, elegidos, tramos)
        e.eventos.append(Evento(creacion, "CREAR", {"envio": e.n}))

        if rng.random() < 0.04:                              # unos pocos se cancelan antes de salir
            e.cancelado = creacion + H * _entre(rng, 3, 15)
            e.eventos.append(Evento(e.cancelado, "CANCELAR", {"envio": e.n}))
            e.liberacion = e.cancelado
        else:
            # demoras: las chicas en cualquier tramo; las grandes, en el último
            for t in tramos[:-1]:
                t.llegada_real = t.llegada_estimada + H * _entre(rng, -2, 4)
            demora = _entre(rng, -6, 8) + (_entre(rng, 24, 96) if rng.random() < prob_demora else 0)
            ultimo.llegada_real = max(ultimo.salida + H, ultimo.llegada_estimada + H * demora)
            forzado = False
            # Para que "hoy" haya un envío en cada estado: uno que acaba de llegar y está en aduana,
            # y otro que debía haber llegado y sigue viajando (DEMORADO)
            if ("aduana" not in forzados and ultimo.aduana and ultimo.salida < ahora - 6 * H
                    and ahora - 72 * H < ultimo.llegada_estimada < ahora + 48 * H):
                ultimo.llegada_real = ahora - H * _entre(rng, 1, 5)
                ultimo.horas_aduana = max(ultimo.horas_aduana, 14.0)
                forzados.add("aduana")
                forzado = True
            elif "demora" not in forzados and ahora - 120 * H < ultimo.llegada_estimada < ahora - 13 * H:
                ultimo.llegada_real = ahora + H * _entre(rng, 24, 72)
                forzados.add("demora")
                forzado = True
            incidentes = _incidentes(rng, e, tipos_inc, inc_promedio)
            for inc in incidentes:                           # un incidente serio en el último tramo demora más
                if forzado:
                    break
                if inc["tramo"] == len(tramos) - 1 and (inc["severidad"] >= 3 or inc["tipo"] in ("CLIMA", "DEMORA_ADUANA")):
                    ultimo.llegada_real += H * _entre(rng, 12, 48)
            for t in tramos:
                t.llegada_real = t.llegada_real.replace(second=0, microsecond=0)
            cambios = _estados(e, rng)
            validar_estados(cambios)
            for cuando, estado in cambios:
                e.eventos.append(Evento(cuando, "ESTADO", {"envio": e.n, "estado": estado}))
            for i, t in enumerate(tramos):
                e.eventos.append(Evento(t.llegada_real, "LLEGADA", {"envio": e.n, "tramo": i}))
            for k, inc in enumerate(incidentes):
                e.eventos.append(Evento(inc["momento"], "INCIDENTE", {"envio": e.n, "incidente": k, **inc}))
                cuando = inc["momento"]
                for estado, desde_h, hasta_h in (("EN_ANALISIS", 1, 12), ("RESUELTO", 24, 96), ("CERRADO", 24, 72)):
                    cuando += H * _entre(rng, desde_h, hasta_h)
                    e.eventos.append(Evento(cuando, "INCIDENTE_ESTADO", {"envio": e.n, "incidente": k, "estado": estado}))
            e.liberacion = cambios[-1][0] if cambios[-1][1] == "ENTREGADO" else None
        if e.liberacion is not None and e.liberacion > ahora:
            e.liberacion = None                              # todavía viaja: sus contenedores siguen ocupados
        for i in elegidos:
            libre_desde[i] = e.liberacion or datetime.max.replace(tzinfo=ahora.tzinfo)
        envios.append(e)

    paso = _agregar_lecturas(envios, contenedores, ahora)
    generales = _eventos_generales(rng, inicio, ahora, envios)
    todos = [ev for e in envios for ev in e.eventos] + generales
    for i, ev in enumerate(todos):
        ev.orden = i
    todos.sort(key=lambda ev: (ev.momento, _PRIORIDAD.get(ev.tipo, 5), ev.orden))
    return Plan(inicio, ahora, contenedores, envios, todos, paso)


# A la misma hora: primero se crea, después se leen sensores, después llega y cambia de estado
_PRIORIDAD = {"CIERRE": 0, "CREAR": 1, "LECTURAS": 2, "LLEGADA": 3, "ESTADO": 4, "CANCELAR": 4}


def _agregar_lecturas(envios: list[EnvioPlan], contenedores: list[ContenedorPlan], ahora: datetime) -> int:
    """Lecturas de los sensores de cada envío mientras viaja, en lotes de un día. El paso (cada
    cuántas horas) se elige para que el total ronde LECTURAS_OBJETIVO."""
    ventanas = []
    for e in envios:
        sensores = sum(len(contenedores[i].sensores) for i in e.contenedores)
        if e.cancelado or not sensores:
            continue
        desde, hasta = e.tramos[0].salida, min(e.tramos[-1].llegada_real, ahora)
        if hasta > desde:
            ventanas.append((e, desde, hasta, sensores))
    horas = sum((h - d) / H * s for _, d, h, s in ventanas)
    paso = max(1, math.ceil(horas / LECTURAS_OBJETIVO))
    for e, desde, hasta, _ in ventanas:
        momentos, t = [], desde + paso * H
        while t <= hasta:
            momentos.append(t)
            t += paso * H
        for i in range(0, len(momentos), max(1, 24 // paso)):
            lote = momentos[i:i + max(1, 24 // paso)]
            e.eventos.append(Evento(lote[-1], "LECTURAS", {"envio": e.n, "momentos": lote}))
    return paso


def _eventos_generales(rng: random.Random, inicio: datetime, ahora: datetime, envios: list[EnvioPlan]) -> list[Evento]:
    eventos = []
    dia = (inicio + timedelta(days=1)).replace(hour=6, minute=0)
    while dia <= ahora:
        eventos.append(Evento(dia, "VENCIDAS", {}))
        if dia.day == 1:
            anterior = (dia - timedelta(days=1)).replace(day=1, hour=0)
            periodo = anterior.strftime("%Y-%m")
            cero = dia.replace(hour=0)
            eventos.append(Evento(cero + H, "CIERRE", {"periodo": periodo, "desde": anterior, "hasta": cero}))
            eventos.append(Evento(cero + 9 * H, "FACTURAR", {}))
            eventos.append(Evento(cero + 34 * H, "RECIBIDAS", {"periodo": periodo, "desde": anterior, "hasta": cero}))
        dia += timedelta(days=1)

    # Mensajería: un grupo de trabajo con mensajes cada pocos días, y consultas de clientes por envíos demorados
    eventos.append(Evento(inicio + 2 * H, "GRUPO", {}))
    t = inicio + timedelta(days=1)
    while t < ahora:
        eventos.append(Evento(t.replace(second=0, microsecond=0), "MENSAJE_GRUPO",
                              {"autor": rng.choice(["olga", "martin", "paula"]), "texto": rng.choice(TEXTOS_GRUPO)}))
        t += timedelta(days=_entre(rng, 3, 7), hours=_entre(rng, 0, 10))
    for e in envios:
        usuario = USUARIO_DE_CLIENTE.get(e.cliente)
        demora = next((ev.momento for ev in e.eventos if ev.tipo == "ESTADO" and ev.datos["estado"] == "DEMORADO"), None)
        if usuario and demora:
            pregunta = demora + H * _entre(rng, 1, 8)
            eventos.append(Evento(pregunta, "MENSAJE_PRIVADO", {"de": usuario, "para": "olga", "envio": e.n,
                                                                 "texto": "Hola, ¿tienen novedades del envío {id}? "
                                                                          "Figura demorado."}))
            eventos.append(Evento(pregunta + H * _entre(rng, 0.5, 4), "MENSAJE_PRIVADO",
                                  {"de": "olga", "para": usuario, "envio": e.n,
                                   "texto": "Hola, sí: el envío {id} viene con atraso en el último tramo. "
                                            "Te aviso apenas tengamos la nueva fecha de llegada."}))
    return eventos


TEXTOS_GRUPO = [
    "Recuerden cargar las llegadas de los tramos el mismo día, así la trazabilidad queda al día.",
    "El puerto de Santos sigue con demoras en aduana; conviene avisar a los clientes con envíos a Brasil.",
    "Pronóstico de nieve en el paso a Chile: revisemos los envíos terrestres a Valparaíso.",
    "Ya está la facturación del mes; revisen si algún envío entregado quedó sin facturar.",
    "Ojo con los reefer: hubo alertas de temperatura, revisemos los sensores antes de cargar.",
    "Mañana sale el buque a Rotterdam, confirmen los contenedores asignados.",
    "Actualicé las tarifas de referencia del transporte aéreo para la próxima cotización.",
    "Frigorífico Pampeano pidió prioridad ALTA para los próximos envíos a Hamburgo.",
]
