"""Verificador de consistencia entre motores (fase 8).

En una arquitectura políglota los mismos hechos quedan en varios motores: un envío está en
PostgreSQL (fuente de verdad), en Neo4j (proyección) y en MongoDB (línea de tiempo); el último
contacto de un sensor está en Redis y su última lectura en Cassandra. La sincronización es
EVENTUAL: durante unos segundos pueden diferir. Este servicio compara los motores y dice:

    OK           coinciden
    ADVERTENCIA  difieren, pero hay eventos en camino (consistencia eventual) o es algo que un
                 proceso periódico corrige solo
    ERROR        difieren y nada en camino lo va a arreglar
    SIN DATOS    un motor no respondió: no se pudo comparar

No modifica nada: solo lee.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

OK, ADVERTENCIA, ERROR, SIN_DATOS = "OK", "ADVERTENCIA", "ERROR", "SIN DATOS"


@dataclass
class Control:
    nombre: str
    motores: str
    estado: str = OK
    detalle: str = ""
    ejemplos: list = field(default_factory=list)


def _ejemplos(conjunto, n: int = 5) -> list:
    return sorted(conjunto)[:n]


class ServicioVerificacion:
    def __init__(self, repo_controles, repo_outbox, colas: list[tuple[str, object]], repo_grafo, repo_eventos,
                 repo_incidentes, repo_telemetria, repo_telemetria_redis, repo_contenedores,
                 repo_historial, repo_sesiones, repo_reportes=None):
        self._sql = repo_controles
        self._outbox = repo_outbox
        self._colas = colas                       # [("Neo4j", cola grupo sincronizadores), ("MongoDB", ...)]
        self._grafo = repo_grafo
        self._eventos = repo_eventos
        self._incidentes = repo_incidentes
        self._cassandra = repo_telemetria
        self._redis_tel = repo_telemetria_redis
        self._contenedores = repo_contenedores
        self._historial = repo_historial
        self._sesiones = repo_sesiones
        self._reportes = repo_reportes          # fase 9 (None = el control no se hace)

    def verificar(self) -> list[Control]:
        controles = [
            ("Outbox publicado", "PostgreSQL → Redis", self._outbox_publicado),
            ("Stream de sincronización", "Redis", self._stream),
            ("Envíos en el grafo", "PostgreSQL ↔ Neo4j", self._envios_en_grafo),
            ("Línea de tiempo de los envíos", "PostgreSQL ↔ MongoDB", self._linea_de_tiempo),
            ("Incidentes en el grafo", "MongoDB ↔ Neo4j", self._incidentes_en_grafo),
            ("Último contacto de los sensores", "Cassandra ↔ Redis", self._ultimo_contacto),
            ("Saldos de cuentas corrientes", "PostgreSQL", self._saldos),
            ("Estados de las facturas", "PostgreSQL", self._facturas),
            ("Cierres mensuales", "PostgreSQL", self._cierres),
            ("Contenedores en uso", "PostgreSQL", self._contenedores_ok),
            ("Sesiones activas", "PostgreSQL ↔ Redis", self._sesiones_ok),
        ]
        if self._reportes is not None:
            controles.append(("Reportes cobrados", "MongoDB ↔ PostgreSQL", self._reportes_cobrados))
        resultado = []
        for nombre, motores, controlar in controles:
            c = Control(nombre, motores)
            try:
                controlar(c)
            except Exception as exc:  # noqa: BLE001   (un motor caído no frena los otros controles)
                log.warning("Control '%s' sin datos: %s", nombre, exc)
                c.estado, c.detalle, c.ejemplos = SIN_DATOS, f"no respondió un motor ({type(exc).__name__})", []
            resultado.append(c)
        return resultado

    # ------------------------------------------------------------------ sincronización
    def _pendientes(self) -> int:
        try:
            return self._outbox.contar_pendientes() + self._incidentes.contar_pendientes_grafo() + sum(
                cola.estado()["sin_confirmar"] for _, cola in self._colas)
        except Exception:  # noqa: BLE001
            return 0

    def _outbox_publicado(self, c: Control) -> None:
        n = self._outbox.contar_pendientes()
        if n:
            c.estado, c.detalle = ADVERTENCIA, f"{n} eventos esperan al sincronizador (consistencia eventual)"
        else:
            c.detalle = "todos los eventos del outbox están en el stream"

    def _stream(self, c: Control) -> None:
        partes, fallidos = [], 0
        for nombre, cola in self._colas:
            e = cola.estado()
            fallidos = max(fallidos, e["fallidos"])      # sync:fallidos es uno solo para todos los grupos
            partes.append(f"{nombre}: {e['sin_confirmar']} sin confirmar")
            if e["sin_confirmar"]:
                c.estado = ADVERTENCIA
        if fallidos:
            c.estado = ERROR
            partes.append(f"{fallidos} eventos en sync:fallidos (necesitan revisión)")
        c.detalle = "; ".join(partes)

    def _envios_en_grafo(self, c: Control) -> None:
        pg, neo = self._sql.estados_envios(), self._grafo.estados_envios()
        faltan = set(pg) - set(neo)
        distintos = {i for i in set(pg) & set(neo) if pg[i] != neo[i]}
        sobran = set(neo) - set(pg)
        if faltan or distintos:
            c.estado = ADVERTENCIA if self._pendientes() else ERROR
            c.detalle = f"{len(faltan)} envíos faltan en Neo4j y {len(distintos)} tienen otro estado"
            c.ejemplos = _ejemplos(faltan | distintos)
        elif sobran:
            c.estado = ADVERTENCIA
            c.detalle = f"{len(sobran)} envíos están en Neo4j pero no en PostgreSQL (reconstruir el grafo los limpia)"
            c.ejemplos = _ejemplos(sobran)
        else:
            c.detalle = f"los {len(pg)} envíos coinciden, con el mismo estado"

    def _linea_de_tiempo(self, c: Control) -> None:
        pg = self._sql.estados_envios()
        sin_inicio = set(pg) - self._eventos.envios_con_evento("CARGA_INICIADA")
        entregados = {i for i, e in pg.items() if e == "ENTREGADO"}
        sin_entrega = entregados - self._eventos.envios_con_evento("ENTREGADO")
        if sin_inicio or sin_entrega:
            c.estado = ADVERTENCIA if self._pendientes() else ERROR
            c.detalle = (f"{len(sin_inicio)} envíos sin evento CARGA_INICIADA y {len(sin_entrega)} entregados "
                         "sin evento ENTREGADO")
            c.ejemplos = _ejemplos(sin_inicio | sin_entrega)
        else:
            c.detalle = f"los {len(pg)} envíos tienen su línea de tiempo ({len(entregados)} con la entrega)"

    def _incidentes_en_grafo(self, c: Control) -> None:
        mongo, neo = self._incidentes.contar(), self._grafo.contar_incidentes()
        pendientes = self._incidentes.contar_pendientes_grafo()
        if mongo != neo:
            c.estado = ADVERTENCIA if (pendientes or self._pendientes()) else ERROR
            c.detalle = f"MongoDB tiene {mongo} y Neo4j {neo} ({pendientes} marcados pendiente_grafo)"
        else:
            c.detalle = f"{mongo} incidentes en los dos motores"

    def _ultimo_contacto(self, c: Control) -> None:
        ids = [s.id_sensor for s in self._contenedores.sensores_instalados_activos()]
        contactos = self._redis_tel.contactos()
        distintos, sin_redis = set(), set()
        for f in self._cassandra.ultimas_lecturas(ids):
            ts = f["fecha_hora"].timestamp()
            if f["id_sensor"] not in contactos:
                sin_redis.add(f["id_sensor"])
            elif abs(contactos[f["id_sensor"]] - ts) > 1:
                distintos.add(f["id_sensor"])
        if distintos or sin_redis:
            c.estado = ADVERTENCIA
            c.detalle = (f"{len(distintos)} sensores con otra fecha y {len(sin_redis)} sin contacto en Redis "
                         "(la próxima lectura lo corrige)")
            c.ejemplos = _ejemplos(distintos | sin_redis)
        else:
            c.detalle = f"{len(ids)} sensores instalados: la última lectura coincide con el último contacto"

    # ------------------------------------------------------------------ PostgreSQL por dentro
    @staticmethod
    def _lista(c: Control, filas: list[dict], bien: str, mal: str, clave: str) -> None:
        if filas:
            c.estado, c.detalle = ERROR, f"{len(filas)} {mal}"
            c.ejemplos = [f[clave] for f in filas[:5]]
        else:
            c.detalle = bien

    def _saldos(self, c: Control) -> None:
        self._lista(c, self._sql.cuentas_descuadradas(),
                    "cada saldo es la suma de sus movimientos", "cuentas con el saldo descuadrado",
                    "razon_social")

    def _facturas(self, c: Control) -> None:
        self._lista(c, self._sql.facturas_descuadradas(),
                    "el estado de cada factura coincide con sus pagos", "facturas con estado incorrecto",
                    "numero")

    def _cierres(self, c: Control) -> None:
        filas = self._sql.cierres_que_no_encadenan() + self._sql.cierres_desactualizados()
        self._lista(c, filas,
                    "cada mes empieza donde terminó el anterior y ningún mes cerrado cambió",
                    "cierres que no cuadran", "periodo")

    def _contenedores_ok(self, c: Control) -> None:
        self._lista(c, self._sql.contenedores_inconsistentes(),
                    "cada contenedor EN_USO está en un solo envío en curso",
                    "contenedores con el estado incorrecto", "codigo_internacional")

    def _sesiones_ok(self, c: Control) -> None:
        activas = self._historial.activas()
        vencidas = [f["id_sesion"] for f in activas if not self._sesiones.existe(f["id_sesion"])]
        if vencidas:
            c.estado = ADVERTENCIA
            c.detalle = (f"{len(vencidas)} sesiones figuran ACTIVA en PostgreSQL pero ya vencieron en Redis "
                         "(las corrige el proceso 'sesiones')")
            c.ejemplos = [v[:8] for v in vencidas[:5]]
        else:
            c.detalle = f"{len(activas)} sesiones activas, todas con su clave en Redis"

    def _reportes_cobrados(self, c: Control) -> None:
        """Fase 9: un reporte cobrado tiene que figurar en una factura (PostgreSQL) Y tener la marca en MongoDB."""
        pg, mongo = self._sql.reportes_en_facturas(), self._reportes.ids_facturados()
        sin_marca, sin_factura = pg - mongo, mongo - pg
        if sin_factura:
            c.estado, c.detalle = ERROR, f"{len(sin_factura)} reportes marcados como cobrados sin factura"
            c.ejemplos = _ejemplos(sin_factura)
        elif sin_marca:
            c.estado = ADVERTENCIA
            c.detalle = (f"{len(sin_marca)} reportes cobrados que MongoDB todavía no marcó "
                         "(lo corrige la próxima facturación)")
            c.ejemplos = _ejemplos(sin_marca)
        else:
            c.detalle = f"{len(pg)} reportes cobrados, con la misma marca en los dos motores"
