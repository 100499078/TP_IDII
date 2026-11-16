"""Procesos periódicos (fase 7): qué corre, cada cuánto y con qué protección.

Cada proceso:
- tiene un intervalo; el planificador lo corre cuando pasó ese tiempo desde la última vez
  (la última ejecución se guarda en Redis, en proceso:{nombre});
- se protege con un lock de Redis: si ya está corriendo (en otra terminal u otra PC), no arranca;
- es IDEMPOTENTE: correrlo de más no rompe nada (el cierre de un mes ya cerrado no hace nada,
  una factura ya VENCIDA no cambia, una alerta repetida se silencia, etc.).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from app.excepciones import EntidadNoEncontrada

log = logging.getLogger(__name__)


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Proceso:
    nombre: str
    descripcion: str
    cada: timedelta
    funcion: Callable[[], str]          # devuelve un texto con lo que hizo


@dataclass
class ResultadoProceso:
    nombre: str
    ok: bool
    resultado: str
    segundos: float = 0.0


def mes_anterior(ahora: datetime) -> tuple[str, datetime, datetime]:
    """('2026-09', 1/9 00:00 UTC, 1/10 00:00 UTC) si hoy es cualquier día de octubre de 2026."""
    hasta = ahora.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    desde = (hasta - timedelta(days=1)).replace(day=1)
    return desde.strftime("%Y-%m"), desde, hasta


class ServicioProcesos:
    def __init__(self, procesos: list[Proceso], repo_estado, repo_locks, reloj: Callable[[], datetime] = _ahora):
        self.procesos = {p.nombre: p for p in procesos}
        self._estado = repo_estado
        self._locks = repo_locks
        self._reloj = reloj

    def ejecutar(self, nombre: str) -> ResultadoProceso:
        proceso = self.procesos.get(nombre)
        if proceso is None:
            raise EntidadNoEncontrada(f"No existe el proceso {nombre}")
        # El lock vence solo (el doble del intervalo, con un tope) por si el proceso se cae a la mitad
        segundos_lock = int(min(max(proceso.cada.total_seconds() * 2, 60), 3600))
        token = self._locks.tomar(f"periodico:{nombre}", segundos_lock)
        if token is None:
            return ResultadoProceso(nombre, False, "ya se está ejecutando en otro lado (lock tomado)")
        inicio = self._reloj()
        try:
            resultado = ResultadoProceso(nombre, True, proceso.funcion())
        except Exception as exc:  # noqa: BLE001   (se registra y se reintenta en la próxima vuelta)
            log.exception("Falló el proceso %s", nombre)
            resultado = ResultadoProceso(nombre, False, f"ERROR {type(exc).__name__}: {exc}")
        finally:
            self._locks.soltar(f"periodico:{nombre}", token)
        fin = self._reloj()
        resultado.segundos = round((fin - inicio).total_seconds(), 2)
        try:
            self._estado.registrar(nombre, inicio, fin, resultado.ok, resultado.resultado)
        except Exception as exc:  # noqa: BLE001
            log.warning("No se pudo guardar el estado del proceso %s en Redis: %s", nombre, exc)
        log.info("Proceso %s: %s (%.2f s)", nombre, resultado.resultado, resultado.segundos)
        return resultado

    def pendientes(self) -> list[str]:
        """Los procesos a los que ya les toca correr otra vez."""
        ahora = self._reloj()
        listos = []
        for p in self.procesos.values():
            ultimo = self._estado.ultimo(p.nombre)
            if ultimo is None or ahora - ultimo["inicio"] >= p.cada:
                listos.append(p.nombre)
        return listos

    def estado(self) -> list[dict]:
        ahora = self._reloj()
        filas = []
        for p in self.procesos.values():
            ultimo = self._estado.ultimo(p.nombre)
            proxima = (ultimo["inicio"] + p.cada) if ultimo else ahora
            filas.append({"proceso": p, "ultimo": ultimo, "proxima": max(proxima, ahora)})
        return filas
