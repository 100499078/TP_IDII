"""Telemetría IoT en Cassandra (keyspace logistica_iot).

En Cassandra se modela POR CONSULTA: no hay JOIN, así que cada consulta tiene su
propia tabla y la misma lectura se escribe en varias (desnormalización):

    Q1 lecturas_por_sensor        clave ((id_sensor, dia), fecha_hora)
    Q2 lecturas_por_contenedor    clave ((id_contenedor, dia), fecha_hora, id_sensor)
    Q3 posiciones_por_envio       clave (id_envio, fecha_hora, id_contenedor)
    Q4 ultima_lectura_por_sensor  clave (id_sensor)            -> se sobrescribe (upsert)
    Q5 anomalias_temperatura      clave (dia, fecha_hora, id_sensor)

Las escrituras de una lectura van en un BATCH LOGGED: Cassandra garantiza que, si
una parte se aplica, se terminan aplicando todas (atomicidad, no aislamiento).
El "día" en la clave de partición evita particiones gigantes: cada sensor
genera una partición nueva por día.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from app.dominio.modelos import Lectura
from app.excepciones import ErrorConexion
from app.infra.conexiones import GestorConexiones

_COLUMNAS = "temperatura, humedad, vibracion, latitud, longitud, bateria"

INSERTAR_POR_SENSOR = f"""
INSERT INTO lecturas_por_sensor (id_sensor, dia, fecha_hora, id_contenedor, {_COLUMNAS})
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""

INSERTAR_POR_CONTENEDOR = f"""
INSERT INTO lecturas_por_contenedor (id_contenedor, dia, fecha_hora, id_sensor, {_COLUMNAS})
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""

INSERTAR_POSICION = """
INSERT INTO posiciones_por_envio (id_envio, fecha_hora, id_contenedor, latitud, longitud)
VALUES (?, ?, ?, ?, ?)"""

# Upsert con "USING TIMESTAMP": si llega una lectura VIEJA después de una nueva
# (p. ej. al cargar un lote histórico), no pisa la última lectura real.
ACTUALIZAR_ULTIMA = """
INSERT INTO ultima_lectura_por_sensor (id_sensor, id_contenedor, fecha_hora, temperatura, humedad,
                                       bateria, latitud, longitud)
VALUES (?, ?, ?, ?, ?, ?, ?, ?) USING TIMESTAMP ?"""

INSERTAR_ANOMALIA = """
INSERT INTO anomalias_temperatura (dia, fecha_hora, id_sensor, id_contenedor, id_envio, temperatura,
                                   umbral_min, umbral_max)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)"""

LECTURAS_SENSOR = f"""
SELECT fecha_hora, id_contenedor, {_COLUMNAS} FROM lecturas_por_sensor
WHERE id_sensor = ? AND dia = ? LIMIT ?"""

LECTURAS_CONTENEDOR = f"""
SELECT fecha_hora, id_sensor, {_COLUMNAS} FROM lecturas_por_contenedor
WHERE id_contenedor = ? AND dia = ? LIMIT ?"""

RECORRIDO_ENVIO = """
SELECT fecha_hora, id_contenedor, latitud, longitud FROM posiciones_por_envio
WHERE id_envio = ? LIMIT ?"""

# ORDER BY invertido: con la partición fija se puede leer la clustering al revés (la más reciente)
ULTIMA_POSICION = """
SELECT fecha_hora, id_contenedor, latitud, longitud FROM posiciones_por_envio
WHERE id_envio = ? ORDER BY fecha_hora DESC, id_contenedor DESC LIMIT 1"""

ULTIMA_LECTURA = """
SELECT id_sensor, id_contenedor, fecha_hora, temperatura, humedad, bateria, latitud, longitud
FROM ultima_lectura_por_sensor WHERE id_sensor IN ?"""

ANOMALIAS_DIA = """
SELECT fecha_hora, id_sensor, id_contenedor, id_envio, temperatura, umbral_min, umbral_max
FROM anomalias_temperatura WHERE dia = ? LIMIT ?"""


def _dec(valor: float | None) -> Decimal | None:
    """Las columnas `decimal` de Cassandra se escriben con Decimal (2 decimales alcanzan)."""
    return None if valor is None else Decimal(str(round(valor, 2)))


def _utc(valor: datetime | None) -> datetime | None:
    """El driver devuelve fechas sin zona horaria (son UTC): se la agregamos."""
    return None if valor is None or valor.tzinfo else valor.replace(tzinfo=timezone.utc)


def _fila(f) -> dict:
    """Convierte una fila del driver en dict con float en lugar de Decimal y fechas con zona."""
    d = f._asdict()
    for k, v in d.items():
        if isinstance(v, Decimal):
            d[k] = float(v)
        elif isinstance(v, datetime):
            d[k] = _utc(v)
    return d


class RepositorioTelemetria:
    def __init__(self, conexiones: GestorConexiones):
        self._cx = conexiones
        self._preparadas: dict[str, object] = {}

    # Las sentencias se PREPARAN una vez: Cassandra las analiza y después solo
    # recibe los valores (más rápido y sin riesgo de inyección).
    def _p(self, cql: str):
        if cql not in self._preparadas:
            self._preparadas[cql] = self._sesion.prepare(cql)
        return self._preparadas[cql]

    @property
    def _sesion(self):
        return self._cx.cassandra

    def _ejecutar(self, sentencia, valores=None):
        # Solo los errores de "no responde" se traducen; un error de CQL es un bug y se deja ver
        from cassandra import OperationTimedOut, ReadTimeout, Unavailable, WriteTimeout
        from cassandra.cluster import NoHostAvailable
        try:
            return self._sesion.execute(sentencia, valores, timeout=10)
        except (NoHostAvailable, OperationTimedOut, Unavailable, ReadTimeout, WriteTimeout, OSError) as exc:
            raise ErrorConexion("Cassandra", type(exc).__name__) from exc

    # ------------------------------------------------------------------ escritura
    def registrar_lectura(self, l: Lectura) -> None:
        from cassandra.query import BatchStatement, BatchType
        valores = (_dec(l.temperatura), _dec(l.humedad), _dec(l.vibracion), l.latitud, l.longitud, l.bateria)
        lote = BatchStatement(batch_type=BatchType.LOGGED)
        lote.add(self._p(INSERTAR_POR_SENSOR), (l.id_sensor, l.dia, l.fecha_hora, l.id_contenedor, *valores))
        lote.add(self._p(INSERTAR_POR_CONTENEDOR), (l.id_contenedor, l.dia, l.fecha_hora, l.id_sensor, *valores))
        if l.id_envio is not None and l.tiene_posicion:
            lote.add(self._p(INSERTAR_POSICION), (l.id_envio, l.fecha_hora, l.id_contenedor, l.latitud, l.longitud))
        micros = int(l.fecha_hora.timestamp() * 1_000_000)        # gana la lectura más reciente
        lote.add(self._p(ACTUALIZAR_ULTIMA), (l.id_sensor, l.id_contenedor, l.fecha_hora, _dec(l.temperatura),
                                               _dec(l.humedad), l.bateria, l.latitud, l.longitud, micros))
        self._ejecutar(lote)

    def registrar_anomalia(self, l: Lectura, umbral_min: float, umbral_max: float) -> None:
        self._ejecutar(self._p(INSERTAR_ANOMALIA), (l.dia, l.fecha_hora, l.id_sensor, l.id_contenedor, l.id_envio,
                                                   _dec(l.temperatura), _dec(umbral_min), _dec(umbral_max)))

    # ------------------------------------------------------------------ consultas
    def lecturas_sensor(self, id_sensor: int, dia: date, limite: int = 200) -> list[dict]:
        return [_fila(f) for f in self._ejecutar(self._p(LECTURAS_SENSOR), (id_sensor, dia, limite))]

    def lecturas_contenedor(self, id_contenedor: int, dia: date, limite: int = 500) -> list[dict]:
        return [_fila(f) for f in self._ejecutar(self._p(LECTURAS_CONTENEDOR), (id_contenedor, dia, limite))]

    def recorrido_envio(self, id_envio: int, limite: int = 2000) -> list[dict]:
        return [_fila(f) for f in self._ejecutar(self._p(RECORRIDO_ENVIO), (id_envio, limite))]

    def ultima_posicion_envio(self, id_envio: int) -> dict | None:
        fila = self._ejecutar(self._p(ULTIMA_POSICION), (id_envio,)).one()
        return _fila(fila) if fila else None

    def ultimas_lecturas(self, ids_sensor: list[int]) -> list[dict]:
        if not ids_sensor:
            return []
        return [_fila(f) for f in self._ejecutar(self._p(ULTIMA_LECTURA), (list(ids_sensor),))]

    def anomalias_dia(self, dia: date, limite: int = 200) -> list[dict]:
        return [_fila(f) for f in self._ejecutar(self._p(ANOMALIAS_DIA), (dia, limite))]
