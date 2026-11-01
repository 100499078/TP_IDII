"""Simulador de sensores: como no hay sensores reales, genera lecturas creíbles.

- Temperatura: "caminata aleatoria" alrededor del centro del rango permitido del sensor
  (cada lectura se mueve un poco respecto de la anterior). Con probabilidad
  `prob_anomalia` se fuerza un valor fuera de rango para ver las alertas.
- GPS: si el contenedor viaja en un envío, la posición sale de su RUTA REAL: se busca el
  tramo que corresponde a ese momento y se interpola entre las coordenadas de su
  origen y su destino según el tiempo transcurrido.
- Batería: baja de a poco.

Las lecturas se registran con ServicioTelemetria, igual que las de un sensor real:
pasan por las mismas validaciones y generan las mismas alertas.
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from app.dominio.modelos import Envio, Sensor

log = logging.getLogger(__name__)


@dataclass
class _EstadoSensor:
    temperatura: float | None = None
    humedad: float = 55.0
    bateria: float = 100.0


@dataclass
class ResultadoSimulacion:
    lecturas: int = 0
    alertas: list = field(default_factory=list)
    errores: int = 0


def posicion_en_ruta(envio: Envio, momento: datetime, coordenadas: Callable[[str], tuple[float, float]]
                     ) -> tuple[float, float] | None:
    """Dónde está el envío en `momento`, interpolando dentro del tramo que corresponde."""
    if not envio.tramos:
        return coordenadas(envio.origen)
    for t in envio.tramos:
        llegada = t.fecha_llegada_real or t.fecha_llegada_estimada
        if momento < t.fecha_salida:                 # todavía no salió: está en el origen del tramo
            return coordenadas(t.origen)
        if momento <= llegada:                       # viajando en este tramo
            avance = (momento - t.fecha_salida) / (llegada - t.fecha_salida)
            (la1, lo1), (la2, lo2) = coordenadas(t.origen), coordenadas(t.destino)
            return round(la1 + (la2 - la1) * avance, 5), round(lo1 + (lo2 - lo1) * avance, 5)
    return coordenadas(envio.tramos[-1].destino)     # ya llegó al final de la ruta cargada


class SimuladorSensores:
    def __init__(self, servicio_telemetria, repo_contenedores, repo_envios, repo_catalogos,
                 prob_anomalia: float = 0.05, semilla: int | None = None,
                 reloj: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self._telemetria = servicio_telemetria
        self._pg = repo_contenedores
        self._envios = repo_envios
        self._catalogos = repo_catalogos
        self.prob_anomalia = prob_anomalia
        self._azar = random.Random(semilla)
        self._reloj = reloj
        self._estado: dict[int, _EstadoSensor] = {}
        self._coords: dict[str, tuple[float, float]] = {}

    def _coordenadas(self, codigo: str) -> tuple[float, float]:
        if codigo not in self._coords:
            u = self._catalogos.ubicacion(codigo)
            self._coords[codigo] = (u.latitud, u.longitud)
        return self._coords[codigo]

    def _valores(self, s: Sensor, envio: Envio | None, momento: datetime) -> dict:
        e = self._estado.setdefault(s.id_sensor, _EstadoSensor())
        anomalia = self._azar.random() < self.prob_anomalia
        e.bateria = max(0.0, e.bateria - self._azar.uniform(0, 0.05))
        v: dict = {"bateria": int(e.bateria)}
        if s.tipo in ("TEMPERATURA", "MULTI") and s.temp_min is not None:
            centro, ancho = (s.temp_min + s.temp_max) / 2, (s.temp_max - s.temp_min)
            if e.temperatura is None:
                e.temperatura = centro
            # caminata aleatoria que tiende a volver al centro del rango
            e.temperatura += self._azar.gauss(0, ancho * 0.05) + (centro - e.temperatura) * 0.2
            v["temperatura"] = round(e.temperatura + (ancho * self._azar.choice([-0.9, 0.9]) if anomalia else 0), 2)
        if s.tipo in ("HUMEDAD", "MULTI"):
            e.humedad = min(100.0, max(20.0, e.humedad + self._azar.gauss(0, 1.5)))
            v["humedad"] = round(95.0 if anomalia and s.tipo == "HUMEDAD" else e.humedad, 1)
        if s.tipo in ("VIBRACION", "MULTI"):
            v["vibracion"] = round(3.4 if anomalia and s.tipo == "VIBRACION" else abs(self._azar.gauss(0.3, 0.2)), 2)
        if s.tipo in ("GPS", "MULTI") and envio is not None:
            posicion = posicion_en_ruta(envio, momento, self._coordenadas)
            if posicion:
                v["latitud"], v["longitud"] = posicion
        return v

    def generar(self, momentos: list[datetime], al_registrar: Callable | None = None,
                contenedores: set[int] | None = None) -> ResultadoSimulacion:
        """Una lectura por sensor instalado y activo, en cada uno de los momentos indicados.
        contenedores: si se indica, solo los sensores de esos contenedores (lo usa la carga
        de datos de prueba para generar las lecturas de un envío durante su viaje)."""
        resultado = ResultadoSimulacion()
        self._telemetria.olvidar_cache()                     # por si cambiaron sensores o envíos
        sensores = [s for s in self._pg.sensores_instalados_activos()
                    if contenedores is None or s.id_contenedor in contenedores]
        envios: dict[int, Envio | None] = {}
        for s in sensores:
            if s.id_contenedor not in envios:
                id_envio = self._pg.envio_en_curso_de_contenedor(s.id_contenedor)
                envios[s.id_contenedor] = self._envios.obtener(id_envio) if id_envio else None
        for momento in momentos:
            for s in sensores:
                valores = self._valores(s, envios[s.id_contenedor], momento)
                try:
                    alertas = self._telemetria.registrar_lectura(s.id_sensor, momento, **valores)
                    resultado.lecturas += 1
                    resultado.alertas.extend(alertas)
                    if al_registrar:
                        al_registrar(s, momento, valores, alertas)
                except Exception as exc:  # noqa: BLE001
                    resultado.errores += 1
                    log.warning("Lectura simulada del sensor %s rechazada: %s", s.id_sensor, exc)
                    if al_registrar:
                        al_registrar(s, momento, valores, exc)
        return resultado

    def ahora(self, al_registrar: Callable | None = None) -> ResultadoSimulacion:
        return self.generar([self._reloj()], al_registrar)

    def lote(self, horas: int, cada_minutos: int) -> ResultadoSimulacion:
        """Historial: lecturas desde hace `horas` hasta ahora, una cada `cada_minutos`."""
        fin = self._reloj().replace(second=0, microsecond=0)
        pasos = int(horas * 60 / cada_minutos)
        return self.generar([fin - timedelta(minutes=cada_minutos * i) for i in range(pasos, -1, -1)])
