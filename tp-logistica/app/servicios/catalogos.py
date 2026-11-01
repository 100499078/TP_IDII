"""Ubicaciones, vehículos y tarifas."""
from __future__ import annotations

import logging

from app.dominio.modelos import MEDIO_DE_VEHICULO, TIPOS_UBICACION, TIPOS_VEHICULO, Ubicacion, Vehiculo
from app.dominio.validaciones import validar_locode
from app.excepciones import ErrorValidacion

log = logging.getLogger(__name__)


class ServicioCatalogos:
    def __init__(self, repo_catalogos, repo_actores):
        self._repo = repo_catalogos
        self._actores = repo_actores

    def crear_ubicacion(self, codigo: str, nombre: str, tipo: str, latitud: float, longitud: float) -> int:
        codigo = (codigo or "").strip().upper()
        problema = validar_locode(codigo)
        if problema:
            raise ErrorValidacion(problema)
        if tipo not in TIPOS_UBICACION:
            raise ErrorValidacion(f"Tipo inválido; opciones: {', '.join(TIPOS_UBICACION)}")
        if not -90 <= latitud <= 90 or not -180 <= longitud <= 180:
            raise ErrorValidacion("Latitud entre -90 y 90, longitud entre -180 y 180")
        if not (nombre or "").strip():
            raise ErrorValidacion("El nombre es obligatorio")
        # El país sale del propio código UN/LOCODE (las dos primeras letras)
        id_ubicacion = self._repo.crear_ubicacion(Ubicacion(None, codigo, nombre.strip(), tipo, codigo[:2],
                                                            latitud, longitud))
        log.info("Alta de la ubicación %s", codigo)
        return id_ubicacion

    def listar_ubicaciones(self) -> list[Ubicacion]:
        return self._repo.listar_ubicaciones()

    def crear_vehiculo(self, tipo: str, identificacion: str, id_operador: int) -> int:
        if tipo not in TIPOS_VEHICULO:
            raise ErrorValidacion(f"Tipo inválido; opciones: {', '.join(TIPOS_VEHICULO)}")
        identificacion = (identificacion or "").strip().upper()
        if not identificacion:
            raise ErrorValidacion("La identificación (patente, IMO o matrícula) es obligatoria")
        operador = self._actores.obtener(id_operador)
        if operador is None or operador.tipo != "OPERADOR" or operador.estado != "ACTIVO":
            raise ErrorValidacion("El vehículo debe pertenecer a un operador logístico activo")
        if MEDIO_DE_VEHICULO[tipo] not in operador.medios:
            raise ErrorValidacion(f"El operador no trabaja con transporte {MEDIO_DE_VEHICULO[tipo].lower()}")
        id_vehiculo = self._repo.crear_vehiculo(Vehiculo(None, tipo, identificacion, id_operador))
        log.info("Alta del vehículo %s (%s)", id_vehiculo, identificacion)
        return id_vehiculo

    def listar_vehiculos(self, id_operador: int | None = None) -> list[Vehiculo]:
        return self._repo.listar_vehiculos(id_operador)

    def listar_tarifas(self):
        return self._repo.listar_tarifas()
