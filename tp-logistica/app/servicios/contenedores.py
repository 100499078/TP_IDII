"""Gestión de contenedores y asociación de sensores a contenedores."""
from __future__ import annotations

import logging
from datetime import date

from app.dominio.modelos import TIPOS_CONTENEDOR, TIPOS_SENSOR, Contenedor, Sensor
from app.dominio.validaciones import validar_codigo_contenedor
from app.excepciones import EntidadNoEncontrada, ErrorConflicto, ErrorValidacion

log = logging.getLogger(__name__)

ESTADOS_MANUALES_CONTENEDOR = ("DISPONIBLE", "MANTENIMIENTO", "BAJA")   # EN_USO lo pone el envío
ESTADOS_SENSOR = ("ACTIVO", "INACTIVO", "FALLA", "BAJA")


class ServicioContenedores:
    def __init__(self, repo_contenedores):
        self._repo = repo_contenedores

    # ------------------------------------------------------------------ contenedores
    def crear_contenedor(self, codigo: str, tipo: str, capacidad_m3: float) -> int:
        codigo = (codigo or "").strip().upper()
        problema = validar_codigo_contenedor(codigo)
        if problema:
            raise ErrorValidacion(problema)
        if tipo not in TIPOS_CONTENEDOR:
            raise ErrorValidacion(f"Tipo inválido; opciones: {', '.join(TIPOS_CONTENEDOR)}")
        if capacidad_m3 is None or capacidad_m3 <= 0 or capacidad_m3 > 100:
            raise ErrorValidacion("La capacidad debe estar entre 0 y 100 m³")
        id_contenedor = self._repo.crear_contenedor(Contenedor(None, codigo, tipo, capacidad_m3))
        log.info("Alta del contenedor %s (%s)", id_contenedor, codigo)
        return id_contenedor

    def contenedor(self, id_contenedor: int) -> Contenedor:
        c = self._repo.contenedor(id_contenedor)
        if c is None:
            raise EntidadNoEncontrada(f"No existe el contenedor {id_contenedor}")
        return c

    def cambiar_estado_contenedor(self, id_contenedor: int, estado: str) -> None:
        c = self.contenedor(id_contenedor)
        if estado not in ESTADOS_MANUALES_CONTENEDOR:
            raise ErrorValidacion(f"Estado inválido; opciones: {', '.join(ESTADOS_MANUALES_CONTENEDOR)}")
        if c.estado == "EN_USO":
            raise ErrorConflicto("El contenedor está asignado a un envío en curso")
        if c.estado == "BAJA":
            raise ErrorConflicto("El contenedor está dado de baja")
        self._repo.cambiar_estado_contenedor(id_contenedor, estado)
        log.info("Contenedor %s pasa a %s", id_contenedor, estado)

    def listar_contenedores(self, estado: str | None = None) -> list[Contenedor]:
        return self._repo.listar_contenedores(estado)

    # ------------------------------------------------------------------ sensores
    def crear_sensor(self, tipo: str, fabricante: str, modelo: str | None,
                     temp_min: float | None, temp_max: float | None) -> int:
        if tipo not in TIPOS_SENSOR:
            raise ErrorValidacion(f"Tipo inválido; opciones: {', '.join(TIPOS_SENSOR)}")
        if not (fabricante or "").strip():
            raise ErrorValidacion("El fabricante es obligatorio")
        if tipo in ("TEMPERATURA", "MULTI") and (temp_min is None or temp_max is None):
            raise ErrorValidacion("Un sensor de temperatura necesita la temperatura mínima y máxima permitidas")
        if temp_min is not None and temp_max is not None and temp_min >= temp_max:
            raise ErrorValidacion("La temperatura mínima debe ser menor que la máxima")
        id_sensor = self._repo.crear_sensor(Sensor(None, tipo, fabricante.strip(), modelo, temp_min=temp_min,
                                                   temp_max=temp_max))
        log.info("Alta del sensor %s (%s)", id_sensor, tipo)
        return id_sensor

    def sensor(self, id_sensor: int) -> Sensor:
        s = self._repo.sensor(id_sensor)
        if s is None:
            raise EntidadNoEncontrada(f"No existe el sensor {id_sensor}")
        return s

    def instalar_sensor(self, id_sensor: int, id_contenedor: int, fecha: date | None = None) -> None:
        s = self.sensor(id_sensor)
        c = self.contenedor(id_contenedor)
        if s.estado != "ACTIVO":
            raise ErrorConflicto(f"El sensor está {s.estado}: solo se instalan sensores activos")
        if s.id_contenedor is not None:
            raise ErrorConflicto(f"El sensor ya está instalado en el contenedor {s.codigo_contenedor}")
        if c.estado == "BAJA":
            raise ErrorConflicto("No se puede instalar un sensor en un contenedor dado de baja")
        self._repo.instalar_sensor(id_sensor, id_contenedor, fecha or date.today())
        log.info("Sensor %s instalado en el contenedor %s", id_sensor, id_contenedor)

    def desinstalar_sensor(self, id_sensor: int) -> None:
        s = self.sensor(id_sensor)
        if s.id_contenedor is None:
            raise ErrorConflicto("El sensor no está instalado en ningún contenedor")
        self._repo.instalar_sensor(id_sensor, None, None)
        log.info("Sensor %s desinstalado del contenedor %s", id_sensor, s.id_contenedor)

    def cambiar_estado_sensor(self, id_sensor: int, estado: str) -> None:
        self.sensor(id_sensor)
        if estado not in ESTADOS_SENSOR:
            raise ErrorValidacion(f"Estado inválido; opciones: {', '.join(ESTADOS_SENSOR)}")
        self._repo.cambiar_estado_sensor(id_sensor, estado)

    def listar_sensores(self, id_contenedor: int | None = None) -> list[Sensor]:
        return self._repo.listar_sensores(id_contenedor)
