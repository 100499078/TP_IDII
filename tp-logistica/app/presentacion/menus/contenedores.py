"""Menú de contenedores y sensores IoT (y la asociación entre ambos)."""
from __future__ import annotations

from app.dominio.modelos import TIPOS_CONTENEDOR, TIPOS_SENSOR, Sesion
from app.dominio.validaciones import validar_codigo_contenedor
from app.presentacion import componentes as ui
from app.presentacion.menus.base import MenuBase
from app.servicios.contenedores import ESTADOS_MANUALES_CONTENEDOR, ESTADOS_SENSOR


class MenuContenedores(MenuBase):
    TITULO = "Contenedores y sensores"
    OPCIONES = [
        ("1", "Listar contenedores", "listar_contenedores", 2),
        ("2", "Alta de contenedor", "alta_contenedor", 3),
        ("3", "Cambiar estado de contenedor", "estado_contenedor", 3),
        ("4", "Listar sensores", "listar_sensores", 2),
        ("5", "Alta de sensor", "alta_sensor", 3),
        ("6", "Instalar sensor en contenedor", "instalar", 3),
        ("7", "Desinstalar sensor", "desinstalar", 3),
        ("8", "Cambiar estado de sensor", "estado_sensor", 3),
    ]

    def __init__(self, ejecutar, sesion_actual, servicio_contenedores):
        super().__init__(ejecutar, sesion_actual)
        self._srv = servicio_contenedores

    def listar_contenedores(self, sesion: Sesion) -> None:
        estado = ui.pedir_opcion("Estado", ["TODOS", "DISPONIBLE", "EN_USO", "MANTENIMIENTO", "BAJA"], defecto="TODOS")
        lista = self._srv.listar_contenedores(None if estado == "TODOS" else estado)
        ui.tabla(["ID", "Código", "Tipo", "Capacidad m³", "Estado", "Sensores"],
                 [(c.id_contenedor, c.codigo_internacional, c.tipo, c.capacidad_m3, c.estado, c.sensores)
                  for c in lista], f"{len(lista)} contenedores")

    def alta_contenedor(self, sesion: Sesion) -> None:
        codigo = ui.pedir("Código ISO 6346 (ej. CSQU3054383)",
                          validar=lambda v: validar_codigo_contenedor(v.upper())).upper()
        tipo = ui.pedir_opcion("Tipo", list(TIPOS_CONTENEDOR), defecto="DRY")
        capacidad = ui.pedir_decimal("Capacidad (m³)", minimo=0.1)
        ui.ok(f"Contenedor creado con ID {self._srv.crear_contenedor(codigo, tipo, capacidad)}")

    def estado_contenedor(self, sesion: Sesion) -> None:
        id_contenedor = ui.pedir_entero("ID del contenedor", minimo=1)
        estado = ui.pedir_opcion("Nuevo estado", list(ESTADOS_MANUALES_CONTENEDOR))
        self._srv.cambiar_estado_contenedor(id_contenedor, estado)
        ui.ok(f"Contenedor {id_contenedor} → {estado}")

    def listar_sensores(self, sesion: Sesion) -> None:
        id_contenedor = ui.pedir_entero("ID del contenedor (vacío = todos)", minimo=1, obligatorio=False)
        lista = self._srv.listar_sensores(id_contenedor)
        ui.tabla(["ID", "Tipo", "Fabricante", "Modelo", "Estado", "Contenedor", "Instalado", "Rango °C"],
                 [(s.id_sensor, s.tipo, s.fabricante, s.modelo or "—", s.estado, s.codigo_contenedor or "—",
                   ui.fecha(s.fecha_instalacion),
                   f"{s.temp_min} a {s.temp_max}" if s.temp_min is not None else "—") for s in lista],
                 f"{len(lista)} sensores")

    def alta_sensor(self, sesion: Sesion) -> None:
        tipo = ui.pedir_opcion("Tipo", list(TIPOS_SENSOR))
        fabricante = ui.pedir("Fabricante")
        modelo = ui.pedir("Modelo", obligatorio=False) or None
        tmin = tmax = None
        if tipo in ("TEMPERATURA", "MULTI"):
            tmin = ui.pedir_decimal("Temperatura mínima permitida (°C)")
            tmax = ui.pedir_decimal("Temperatura máxima permitida (°C)")
        ui.ok(f"Sensor creado con ID {self._srv.crear_sensor(tipo, fabricante, modelo, tmin, tmax)}")

    def instalar(self, sesion: Sesion) -> None:
        id_sensor = ui.pedir_entero("ID del sensor", minimo=1)
        id_contenedor = ui.pedir_entero("ID del contenedor", minimo=1)
        self._srv.instalar_sensor(id_sensor, id_contenedor)
        ui.ok(f"Sensor {id_sensor} instalado en el contenedor {id_contenedor}")

    def desinstalar(self, sesion: Sesion) -> None:
        self._srv.desinstalar_sensor(ui.pedir_entero("ID del sensor", minimo=1))
        ui.ok("Sensor desinstalado")

    def estado_sensor(self, sesion: Sesion) -> None:
        id_sensor = ui.pedir_entero("ID del sensor", minimo=1)
        estado = ui.pedir_opcion("Nuevo estado", list(ESTADOS_SENSOR))
        self._srv.cambiar_estado_sensor(id_sensor, estado)
        ui.ok(f"Sensor {id_sensor} → {estado}")
