"""Menú de ubicaciones, vehículos y tarifas."""
from __future__ import annotations

from app.dominio.modelos import TIPOS_UBICACION, TIPOS_VEHICULO, Sesion
from app.dominio.validaciones import validar_locode
from app.presentacion import componentes as ui
from app.presentacion.menus.base import MenuBase


class MenuCatalogos(MenuBase):
    TITULO = "Ubicaciones, vehículos y tarifas"
    OPCIONES = [
        ("1", "Listar ubicaciones", "listar_ubicaciones", 1),
        ("2", "Alta de ubicación", "alta_ubicacion", 4),
        ("3", "Listar vehículos", "listar_vehiculos", 2),
        ("4", "Alta de vehículo", "alta_vehiculo", 3),
        ("5", "Ver tarifas", "listar_tarifas", 2),
    ]

    def __init__(self, ejecutar, sesion_actual, servicio_catalogos):
        super().__init__(ejecutar, sesion_actual)
        self._srv = servicio_catalogos

    def listar_ubicaciones(self, sesion: Sesion) -> None:
        lista = self._srv.listar_ubicaciones()
        ui.tabla(["Código", "Nombre", "Tipo", "País", "Lat", "Lon"],
                 [(u.codigo, u.nombre, u.tipo, u.pais, u.latitud, u.longitud) for u in lista],
                 f"{len(lista)} ubicaciones")

    def alta_ubicacion(self, sesion: Sesion) -> None:
        codigo = ui.pedir("Código UN/LOCODE (ej. ARBUE)", validar=lambda v: validar_locode(v.upper())).upper()
        nombre = ui.pedir("Nombre")
        tipo = ui.pedir_opcion("Tipo", list(TIPOS_UBICACION))
        latitud = ui.pedir_decimal("Latitud")
        longitud = ui.pedir_decimal("Longitud")
        self._srv.crear_ubicacion(codigo, nombre, tipo, latitud, longitud)
        ui.ok(f"Ubicación {codigo} creada")

    def listar_vehiculos(self, sesion: Sesion) -> None:
        id_operador = ui.pedir_entero("ID del operador (vacío = todos)", minimo=1, obligatorio=False)
        lista = self._srv.listar_vehiculos(id_operador)
        ui.tabla(["ID", "Tipo", "Identificación", "Operador", "Estado"],
                 [(v.id_vehiculo, v.tipo, v.identificacion, v.operador, v.estado) for v in lista],
                 f"{len(lista)} vehículos")

    def alta_vehiculo(self, sesion: Sesion) -> None:
        tipo = ui.pedir_opcion("Tipo", list(TIPOS_VEHICULO))
        identificacion = ui.pedir("Identificación (patente, IMO o matrícula)")
        id_operador = ui.pedir_entero("ID del operador logístico", minimo=1)
        ui.ok(f"Vehículo creado con ID {self._srv.crear_vehiculo(tipo, identificacion, id_operador)}")

    def listar_tarifas(self, sesion: Sesion) -> None:
        ui.tabla(["Medio", "Precio base", "Precio por kg", "Desde", "Hasta"],
                 [(t.medio_transporte, ui.dinero(t.precio_base), ui.dinero(t.precio_kg), ui.fecha(t.vigente_desde),
                   ui.fecha(t.vigente_hasta)) for t in self._srv.listar_tarifas()], "Tarifas (costo = base + kg × precio)")
