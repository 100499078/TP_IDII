"""Telemetría IoT: registrar lecturas de sensores, detectar anomalías y emitir alertas.

Cada lectura recorre tres motores, cada uno con su papel:

    1. Cassandra  -> guarda la lectura (en 3-4 tablas, un BATCH). Si falla, la lectura NO se registró.
    2. Redis      -> actualiza el último contacto del sensor (ZADD).
    3. Si hay un valor fuera de rango:
         Cassandra -> anomalía (si es de temperatura)
         MongoDB   -> documento de la alerta (registro durable, se consulta y se resuelve)
         Redis     -> XADD en alertas:stream (aviso inmediato a las consolas de operadores)

Cassandra es la fuente de verdad de la telemetría. Si Redis o MongoDB fallan
DESPUÉS de guardar la lectura, no se pierde la lectura: se registra el problema
en el log y se sigue (las alertas son un efecto secundario).
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from app.dominio.modelos import ESTADOS_ALERTA, Alerta, Lectura, ReglaAlerta, Sensor
from app.excepciones import EntidadNoEncontrada, ErrorConflicto, ErrorConexion, ErrorValidacion

log = logging.getLogger(__name__)

HUMEDAD_MAXIMA = 85.0        # % — por encima, la carga corre riesgo
VIBRACION_MAXIMA = 2.5       # g — golpe fuerte
BATERIA_MINIMA = 15          # %
MINUTOS_SIN_CONTACTO = 30
CACHE_SEGUNDOS = 60          # configuración de sensores (y reglas) leída de PostgreSQL

# Fase 9: las reglas se configuran en PostgreSQL (tabla regla_alerta). Estos son los valores de
# fábrica, que se usan si no hay repositorio de reglas (pruebas) o si PostgreSQL no responde.
REGLAS_DE_FABRICA = {
    "TEMPERATURA": ReglaAlerta("TEMPERATURA", None, "°C", True, "Fuera del rango de cada sensor"),
    "HUMEDAD": ReglaAlerta("HUMEDAD", HUMEDAD_MAXIMA, "%", True, "Humedad por encima del umbral"),
    "VIBRACION": ReglaAlerta("VIBRACION", VIBRACION_MAXIMA, "g", True, "Golpe por encima del umbral"),
    "BATERIA_BAJA": ReglaAlerta("BATERIA_BAJA", BATERIA_MINIMA, "%", True, "Batería por debajo del umbral"),
    "SENSOR_INACTIVO": ReglaAlerta("SENSOR_INACTIVO", MINUTOS_SIN_CONTACTO, "min", True, "Sensor en viaje sin reportar"),
}
RANGOS_UMBRAL = {"HUMEDAD": (1, 100), "VIBRACION": (0.1, 50), "BATERIA_BAJA": (1, 99), "SENSOR_INACTIVO": (1, 1440)}


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


class ServicioTelemetria:
    def __init__(self, repo_contenedores, repo_telemetria, repo_redis, repo_alertas,
                 reloj: Callable[[], datetime] = _ahora, repo_reglas=None):
        self._pg = repo_contenedores
        self._cass = repo_telemetria
        self._redis = repo_redis
        self._alertas = repo_alertas
        self._reloj = reloj
        self._repo_reglas = repo_reglas           # fase 9 (None = reglas de fábrica)
        self._reglas_cache: tuple[float, dict[str, ReglaAlerta]] | None = None
        # Caché en memoria: para cada lectura no conviene ir a PostgreSQL a buscar el sensor
        self._cache: dict[int, tuple[float, Sensor, int | None]] = {}

    # ------------------------------------------------------------------ configuración
    def _sensor(self, id_sensor: int) -> tuple[Sensor, int | None]:
        """(sensor, id_envio en curso de su contenedor), con caché de 60 s."""
        guardado = self._cache.get(id_sensor)
        if guardado and guardado[0] > time.monotonic():
            return guardado[1], guardado[2]
        s = self._pg.sensor(id_sensor)
        if s is None:
            raise EntidadNoEncontrada(f"No existe el sensor {id_sensor}")
        envio = self._pg.envio_en_curso_de_contenedor(s.id_contenedor) if s.id_contenedor else None
        self._cache[id_sensor] = (time.monotonic() + CACHE_SEGUNDOS, s, envio)
        return s, envio

    def olvidar_cache(self) -> None:
        self._cache.clear()
        self._reglas_cache = None

    # ------------------------------------------------------------------ reglas de alerta (fase 9)
    def reglas(self) -> dict[str, ReglaAlerta]:
        """Las reglas vigentes, con caché de 60 s. Si PostgreSQL no responde, las de fábrica."""
        if self._repo_reglas is None:
            return dict(REGLAS_DE_FABRICA)
        if self._reglas_cache and self._reglas_cache[0] > time.monotonic():
            return self._reglas_cache[1]
        try:
            reglas = {**REGLAS_DE_FABRICA, **{r.tipo: r for r in self._repo_reglas.listar()}}
        except Exception as exc:  # noqa: BLE001
            log.warning("No se pudieron leer las reglas de alerta; se usan las de fábrica: %s", exc)
            return dict(REGLAS_DE_FABRICA)
        self._reglas_cache = (time.monotonic() + CACHE_SEGUNDOS, reglas)
        return reglas

    def modificar_regla(self, tipo: str, umbral: float | None, activa: bool) -> None:
        if self._repo_reglas is None:
            raise ErrorConflicto("Las reglas de alerta no están configuradas en la base")
        regla = self.reglas().get(tipo)
        if regla is None:
            raise EntidadNoEncontrada(f"No existe la regla {tipo}")
        if tipo == "TEMPERATURA":
            if umbral is not None:
                raise ErrorValidacion("La temperatura no tiene umbral general: se usa el rango de cada sensor")
        else:
            minimo, maximo = RANGOS_UMBRAL[tipo]
            if umbral is None or not minimo <= umbral <= maximo:
                raise ErrorValidacion(f"El umbral de {tipo} va de {minimo} a {maximo} {regla.unidad}")
        self._repo_reglas.actualizar(tipo, umbral, activa)
        self._reglas_cache = None      # en este proceso rige ya; en los demás, en menos de 60 s
        log.warning("Regla %s: umbral %s, %s", tipo, umbral, "activa" if activa else "desactivada")

    # ------------------------------------------------------------------ registrar
    def registrar_lectura(self, id_sensor: int, fecha_hora: datetime | None = None, temperatura: float | None = None,
                          humedad: float | None = None, vibracion: float | None = None,
                          latitud: float | None = None, longitud: float | None = None,
                          bateria: int | None = None) -> list[Alerta]:
        """Guarda la lectura y devuelve las alertas que generó (puede ser una lista vacía)."""
        sensor, id_envio = self._sensor(id_sensor)
        if sensor.estado != "ACTIVO":
            raise ErrorConflicto(f"El sensor {id_sensor} está {sensor.estado}: no se aceptan lecturas")
        if sensor.id_contenedor is None:
            raise ErrorConflicto(f"El sensor {id_sensor} no está instalado en ningún contenedor")
        fecha_hora = fecha_hora or self._reloj()
        if fecha_hora > self._reloj() + timedelta(minutes=5):
            raise ErrorValidacion("La lectura tiene una fecha futura")
        self._validar_rangos(humedad, vibracion, latitud, longitud, bateria)
        if all(v is None for v in (temperatura, humedad, vibracion, latitud, bateria)):
            raise ErrorValidacion("La lectura no trae ningún valor")

        lectura = Lectura(id_sensor, sensor.id_contenedor, fecha_hora, temperatura, humedad, vibracion,
                          latitud, longitud, bateria, id_envio)
        self._cass.registrar_lectura(lectura)                        # 1) fuente de verdad
        try:
            self._redis.registrar_contacto(id_sensor, fecha_hora)    # 2) último contacto
        except Exception as exc:  # noqa: BLE001
            log.warning("No se pudo actualizar el último contacto del sensor %s en Redis: %s", id_sensor, exc)
        return self._evaluar(sensor, lectura)                        # 3) anomalías y alertas

    @staticmethod
    def _validar_rangos(humedad, vibracion, latitud, longitud, bateria) -> None:
        if humedad is not None and not 0 <= humedad <= 100:
            raise ErrorValidacion("La humedad es un porcentaje entre 0 y 100")
        if vibracion is not None and vibracion < 0:
            raise ErrorValidacion("La vibración no puede ser negativa")
        if bateria is not None and not 0 <= bateria <= 100:
            raise ErrorValidacion("La batería es un porcentaje entre 0 y 100")
        if (latitud is None) != (longitud is None):
            raise ErrorValidacion("La posición necesita latitud y longitud")
        if latitud is not None and (not -90 <= latitud <= 90 or not -180 <= longitud <= 180):
            raise ErrorValidacion("Latitud entre -90 y 90, longitud entre -180 y 180")

    # ------------------------------------------------------------------ anomalías y alertas
    def _evaluar(self, s: Sensor, l: Lectura) -> list[Alerta]:
        candidatas: list[tuple[str, float, float, str]] = []      # (tipo, valor, umbral, mensaje)
        reglas = self.reglas()
        activa = lambda tipo: reglas[tipo].activa  # noqa: E731
        if l.temperatura is not None and s.temp_min is not None and s.temp_max is not None:
            if not s.temp_min <= l.temperatura <= s.temp_max:
                self._cass.registrar_anomalia(l, s.temp_min, s.temp_max)   # la anomalía se guarda siempre
            if activa("TEMPERATURA") and l.temperatura > s.temp_max:
                candidatas.append(("TEMPERATURA_ALTA", l.temperatura, s.temp_max,
                                   f"{l.temperatura:.1f} °C supera el máximo de {s.temp_max:.1f} °C"))
            elif activa("TEMPERATURA") and l.temperatura < s.temp_min:
                candidatas.append(("TEMPERATURA_BAJA", l.temperatura, s.temp_min,
                                   f"{l.temperatura:.1f} °C está debajo del mínimo de {s.temp_min:.1f} °C"))
        humedad, vibracion, bateria = (reglas[k].umbral for k in ("HUMEDAD", "VIBRACION", "BATERIA_BAJA"))
        if activa("HUMEDAD") and l.humedad is not None and l.humedad > humedad:
            candidatas.append(("HUMEDAD", l.humedad, humedad, f"Humedad {l.humedad:.0f} %"))
        if activa("VIBRACION") and l.vibracion is not None and l.vibracion > vibracion:
            candidatas.append(("VIBRACION", l.vibracion, vibracion, f"Golpe de {l.vibracion:.1f} g"))
        if activa("BATERIA_BAJA") and l.bateria is not None and l.bateria < bateria:
            candidatas.append(("BATERIA_BAJA", l.bateria, bateria, f"Batería al {l.bateria} %"))

        emitidas = []
        for tipo, valor, umbral, mensaje in candidatas:
            alerta = Alerta(tipo, l.fecha_hora, l.id_sensor, l.id_contenedor,
                            f"Sensor {l.id_sensor} (contenedor {l.id_contenedor}): {mensaje}",
                            valor, umbral, l.id_envio)
            if self._emitir(alerta):
                emitidas.append(alerta)
        return emitidas

    def _emitir(self, a: Alerta) -> bool:
        """Silencio de 15 min por (tipo, sensor) en Redis -> documento en MongoDB -> aviso en el stream."""
        try:
            if not self._redis.reservar_alerta(a.tipo, a.id_sensor):
                return False                                   # ya avisamos hace poco
        except Exception as exc:  # noqa: BLE001
            log.warning("Redis no responde; la alerta se emite sin control de repetición: %s", exc)
        try:
            self._alertas.insertar(a)
        except Exception as exc:  # noqa: BLE001
            log.error("No se pudo guardar la alerta %s del sensor %s en MongoDB: %s", a.tipo, a.id_sensor, exc)
            try:
                self._redis.liberar_alerta(a.tipo, a.id_sensor)   # la próxima lectura lo vuelve a intentar
            except Exception:  # noqa: BLE001
                pass
            return False
        try:
            self._redis.publicar_alerta({"id_alerta": a.id_alerta, "tipo": a.tipo, "id_sensor": a.id_sensor,
                                         "id_contenedor": a.id_contenedor, "id_envio": a.id_envio,
                                         "valor": a.valor, "mensaje": a.mensaje,
                                         "fecha": a.fecha.isoformat()})
        except Exception as exc:  # noqa: BLE001
            log.warning("La alerta %s quedó en MongoDB pero no se pudo avisar por Redis: %s", a.id_alerta, exc)
        log.info("Alerta %s: %s", a.tipo, a.mensaje)
        return True

    # ------------------------------------------------------------------ consultas
    def lecturas_sensor(self, id_sensor: int, dia: date) -> list[dict]:
        return self._cass.lecturas_sensor(id_sensor, dia)

    def lecturas_contenedor(self, id_contenedor: int, dia: date) -> list[dict]:
        return self._cass.lecturas_contenedor(id_contenedor, dia)

    def recorrido_envio(self, id_envio: int) -> list[dict]:
        return self._cass.recorrido_envio(id_envio)

    def ultimas_lecturas(self, id_contenedor: int | None = None) -> list[dict]:
        """Última lectura de cada sensor instalado (de un contenedor, o de todos)."""
        sensores = [s for s in self._pg.sensores_instalados_activos()
                    if id_contenedor is None or s.id_contenedor == id_contenedor]
        filas = {f["id_sensor"]: f for f in self._cass.ultimas_lecturas([s.id_sensor for s in sensores])}
        return [{"sensor": s, "lectura": filas.get(s.id_sensor)} for s in sensores]

    def anomalias(self, dia: date) -> list[dict]:
        return self._cass.anomalias_dia(dia)

    def sensores_sin_contacto(self, minutos: int) -> list[dict]:
        """Sensores instalados y activos que no reportan hace más de `minutos` (o que nunca reportaron)."""
        limite = self._reloj() - timedelta(minutes=minutos)
        contactos = self._redis.contactos()
        resultado = []
        for s in self._pg.sensores_instalados_activos():
            ultimo = contactos.get(s.id_sensor)
            if ultimo is None or ultimo < limite.timestamp():
                resultado.append({"sensor": s, "ultimo": datetime.fromtimestamp(ultimo, timezone.utc)
                                  if ultimo else None})
        return resultado

    def alertar_sin_contacto(self, minutos: int | None = None) -> list[Alerta]:
        """Proceso periódico (fase 7): una alerta SENSOR_INACTIVO por sensor callado.
        Usa el mismo silencio de 15 minutos que el resto de las alertas, así que correrlo
        seguido no repite avisos. Fase 8: solo alerta los sensores de contenedores que están
        VIAJANDO (en un envío en curso); un contenedor guardado en el depósito no transmite."""
        emitidas = []
        self.olvidar_cache()                                 # el envío de cada contenedor, fresco
        regla = self.reglas()["SENSOR_INACTIVO"]             # fase 9: el umbral sale de la regla
        if not regla.activa:
            return []
        minutos = int(minutos or regla.umbral)
        for f in self.sensores_sin_contacto(minutos):
            s = f["sensor"]
            id_envio = self._sensor(s.id_sensor)[1]
            if id_envio is None:
                continue
            alerta = Alerta("SENSOR_INACTIVO", self._reloj(), s.id_sensor, s.id_contenedor,
                            f"Sensor {s.id_sensor} (contenedor {s.id_contenedor}) sin contacto hace más de {minutos} min",
                            float(minutos), None, id_envio)
            if self._emitir(alerta):
                emitidas.append(alerta)
        return emitidas

    def alertas(self, estados: list[str] | None = None) -> list[Alerta]:
        return self._alertas.listar(estados or ["NUEVA", "VISTA"])

    def cambiar_estado_alerta(self, id_alerta: str, estado: str) -> None:
        if estado not in ESTADOS_ALERTA:
            raise ErrorValidacion(f"Estado inválido; opciones: {', '.join(ESTADOS_ALERTA)}")
        try:
            encontrada = self._alertas.cambiar_estado(id_alerta, estado)
        except ErrorConexion:
            raise
        except Exception as exc:  # noqa: BLE001  (un id con formato inválido)
            raise ErrorValidacion("El ID de la alerta no es válido") from exc
        if not encontrada:
            raise EntidadNoEncontrada(f"No existe la alerta {id_alerta}")

    def alertas_nuevas(self, consumidor: str) -> list[dict]:
        """Avisos del stream que todavía no vio ninguna consola (para mostrar en el menú)."""
        self._redis.asegurar_grupo()
        return self._redis.alertas_nuevas(consumidor)
