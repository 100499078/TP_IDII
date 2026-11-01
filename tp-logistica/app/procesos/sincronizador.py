"""Hilo que ejecuta la sincronización con Neo4j cada pocos segundos, mientras la
aplicación de consola está abierta.

Usa SUS PROPIAS conexiones (otro GestorConexiones): una conexión de psycopg no
debe usarse desde dos hilos a la vez.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

log = logging.getLogger(__name__)


class HiloSincronizacion(threading.Thread):
    def __init__(self, fabrica: Callable[[], tuple], intervalo: float = 3.0, espera_tras_error: float = 15.0):
        """fabrica() devuelve (servicio_sincronizacion, gestor_conexiones) creados para este hilo."""
        super().__init__(name="sincronizador", daemon=True)
        self._fabrica = fabrica
        self._intervalo = intervalo
        self._espera_error = espera_tras_error
        self._detener = threading.Event()
        self._despertar = threading.Event()     # "sincronizar ahora" desde el menú
        self._ultimo_error: str | None = None

    def run(self) -> None:
        servicio, conexiones = self._fabrica()
        log.info("Sincronizador iniciado")
        try:
            while not self._detener.is_set():
                try:
                    servicio.ciclo()
                    if self._ultimo_error:
                        log.info("Sincronización recuperada")
                    self._ultimo_error = None
                    self._esperar(self._intervalo)
                except Exception as exc:  # noqa: BLE001
                    texto = f"{type(exc).__name__}: {exc}"
                    if texto != self._ultimo_error:          # no repetir el mismo error en cada ciclo
                        log.warning("Sincronización detenida temporalmente: %s", texto)
                        self._ultimo_error = texto
                    self._esperar(self._espera_error)
        finally:
            conexiones.cerrar()
            log.info("Sincronizador detenido")

    def _esperar(self, segundos: float) -> None:
        """Duerme hasta que pasen los segundos, o antes si piden despertarlo o detenerlo."""
        self._despertar.wait(segundos)
        self._despertar.clear()

    def despertar(self) -> None:
        self._despertar.set()

    def detener(self) -> None:
        self._detener.set()
        self._despertar.set()
        self.join(timeout=10)
