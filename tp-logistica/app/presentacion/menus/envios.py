"""Menú de envíos y tramos.

Un usuario asociado a un cliente (rol CLIENTE, sesion.id_actor con valor) solo ve
SUS envíos: el filtro se aplica en la consulta, no se confía en lo que escriba.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.dominio.modelos import ESTADOS_ENVIO_EN_CURSO, MEDIOS, PRIORIDADES, TRANSICIONES_ENVIO, Sesion
from app.presentacion import componentes as ui
from app.presentacion.menus.base import MenuBase

ESTADOS_ENVIO = ("CREADO", "EN_TRANSITO", "EN_ADUANA", "DEMORADO", "ENTREGADO", "CANCELADO")


def cliente_de(sesion: Sesion) -> int | None:
    """Si el usuario pertenece a un cliente, devuelve su id: solo puede ver lo suyo.
    (Un usuario SOCIO también tiene id_actor: así, en las pantallas de clientes no ve nada ajeno.)"""
    return sesion.id_actor


def operador_de(sesion: Sesion) -> int | None:
    """Fase 9: si es un usuario SOCIO de un operador, el id del operador (ve los envíos que mueve)."""
    return sesion.id_actor if "SOCIO" in sesion.roles else None


class MenuEnvios(MenuBase):
    TITULO = "Envíos y tramos"
    OPCIONES = [
        ("1", "Listar envíos", "listar", 1),
        ("2", "Ver detalle y ruta", "ver", 1),
        ("3", "Crear envío", "crear", 3),
        ("4", "Agregar tramo a la ruta", "agregar_tramo", 3),
        ("5", "Registrar llegada de un tramo", "registrar_llegada", 3),
        ("6", "Cambiar estado del envío", "cambiar_estado", 3),
        ("7", "Ver la ruta en el grafo (Neo4j)", "ruta_grafo", 2),
        # Fase 9: modificación del envío y ABM de sus contenedores
        ("8", "Modificar prioridad o entrega estimada", "modificar", 3),
        ("9", "Agregar un contenedor al envío", "agregar_contenedor", 3),
        ("10", "Quitar un contenedor del envío", "quitar_contenedor", 3),
    ]

    def __init__(self, ejecutar, sesion_actual, servicio_envios, servicio_contenedores, servicio_catalogos):
        super().__init__(ejecutar, sesion_actual)
        self._srv = servicio_envios
        self._contenedores = servicio_contenedores
        self._catalogos = servicio_catalogos

    def listar(self, sesion: Sesion) -> None:
        estado = ui.pedir_opcion("Estado", ["TODOS", *ESTADOS_ENVIO], defecto="TODOS")
        estado = None if estado == "TODOS" else estado
        filas = (self._srv.listar(estado, id_operador=operador_de(sesion)) if operador_de(sesion)
                 else self._srv.listar(estado, cliente_de(sesion)))
        ui.tabla(["ID", "Cliente", "Origen", "Destino", "Estado", "Prioridad", "Entrega estimada", "Tramos"],
                 [(f["id_envio"], f["cliente"], f["origen"], f["destino"], f["estado"], f["prioridad"],
                   ui.fecha(f["fecha_entrega_estimada"]), f["tramos"]) for f in filas],
                 f"{len(filas)} envíos (los 100 más recientes)")

    def ver(self, sesion: Sesion) -> None:
        id_envio = ui.pedir_entero("ID del envío", minimo=1)
        e = (self._srv.obtener(id_envio, id_operador=operador_de(sesion)) if operador_de(sesion)
             else self._srv.obtener(id_envio, cliente_de(sesion)))
        ui.tabla(["Dato", "Valor"], [
            ("Cliente", f"{e.cliente} (ID {e.id_cliente})"),
            ("Origen → destino", f"{e.origen} → {e.destino}"),
            ("Estado", e.estado), ("Prioridad", e.prioridad),
            ("Peso / volumen", f"{e.peso_kg} kg / {e.volumen_m3} m³"),
            ("Contenedores", ", ".join(map(str, e.contenedores))),
            ("Creado", ui.fecha(e.fecha_creacion)),
            ("Entrega estimada", ui.fecha(e.fecha_entrega_estimada)),
            ("Entrega real", ui.fecha(e.fecha_entrega_real)),
            ("Costo total de la ruta", ui.dinero(e.costo_total)),
        ], f"Envío {e.id_envio}")
        ui.tabla(["#", "Tramo", "Recorrido", "Medio", "Operador (vehículo)", "Salida", "Llegada est.",
                  "Llegada real", "Costo"],
                 [(t.orden, t.id_tramo, f"{t.origen}→{t.destino}", t.medio_transporte,
                   f"{t.operador} ({t.vehiculo})" if t.vehiculo else t.operador,
                   ui.fecha(t.fecha_salida), ui.fecha(t.fecha_llegada_estimada), ui.fecha(t.fecha_llegada_real),
                   ui.dinero(t.costo)) for t in e.tramos], "Ruta (PostgreSQL)")
        if e.en_curso and e.ultimo_destino != e.destino:
            ui.info(f"La ruta cargada llega hasta {e.ultimo_destino}; falta llegar a {e.destino}.")

    def crear(self, sesion: Sesion) -> None:
        id_cliente = ui.pedir_entero("ID del cliente", minimo=1)
        ui.tabla(["Código", "Nombre", "Tipo"],
                 [(u.codigo, u.nombre, u.tipo) for u in self._catalogos.listar_ubicaciones()], "Ubicaciones")
        origen = ui.pedir("Origen (código)").upper()
        destino = ui.pedir("Destino (código)").upper()
        entrega = ui.pedir_fecha_hora("Entrega estimada")
        peso = ui.pedir_decimal("Peso (kg)", minimo=0.01)
        volumen = ui.pedir_decimal("Volumen (m³)", minimo=0.01)
        prioridad = ui.pedir_opcion("Prioridad", list(PRIORIDADES), defecto="MEDIA")
        libres = self._contenedores.listar_contenedores("DISPONIBLE")
        ui.tabla(["ID", "Código", "Tipo", "Capacidad m³", "Sensores"],
                 [(c.id_contenedor, c.codigo_internacional, c.tipo, c.capacidad_m3, c.sensores) for c in libres],
                 "Contenedores disponibles")
        contenedores = ui.pedir_lista_enteros("IDs de contenedores")
        id_envio = self._srv.crear_envio(id_cliente, origen, destino, entrega, peso, volumen, contenedores, prioridad)
        ui.ok(f"Envío {id_envio} creado en estado CREADO. Ahora cargá su ruta con 'Agregar tramo'.")

    def agregar_tramo(self, sesion: Sesion) -> None:
        e = self._srv.obtener(ui.pedir_entero("ID del envío", minimo=1))
        ui.info(f"El tramo sale de {e.ultimo_destino} (donde termina la ruta). Destino final: {e.destino}.")
        medio = ui.pedir_opcion("Medio", list(MEDIOS))
        destino = ui.pedir("Destino del tramo (código)", defecto=e.destino).upper()
        id_operador = ui.pedir_entero("ID del operador logístico", minimo=1)
        vehiculos = self._catalogos.listar_vehiculos(id_operador)
        if vehiculos:
            ui.tabla(["ID", "Tipo", "Identificación", "Estado"],
                     [(v.id_vehiculo, v.tipo, v.identificacion, v.estado) for v in vehiculos], "Vehículos del operador")
        id_vehiculo = ui.pedir_entero("ID del vehículo (vacío = sin asignar)", minimo=1, obligatorio=False)
        desde = e.tramos[-1].fecha_llegada_estimada if e.tramos else e.fecha_creacion
        salida = ui.pedir_fecha_hora("Salida", defecto=ui.fecha(desde + timedelta(hours=1)))
        llegada = ui.pedir_fecha_hora("Llegada estimada")
        id_tramo = self._srv.agregar_tramo(e.id_envio, medio, destino, id_operador, salida, llegada, id_vehiculo)
        ui.ok(f"Tramo {id_tramo} agregado. El costo se calculó con la tarifa vigente de transporte {medio.lower()}.")

    def registrar_llegada(self, sesion: Sesion) -> None:
        id_tramo = ui.pedir_entero("ID del tramo", minimo=1)
        fecha = ui.pedir_fecha_hora("Llegada real", defecto=ui.fecha(datetime.now(timezone.utc)))
        self._srv.registrar_llegada(id_tramo, fecha)
        ui.ok("Llegada registrada (la demora alimenta la ruta en el grafo)")

    def cambiar_estado(self, sesion: Sesion) -> None:
        e = self._srv.obtener(ui.pedir_entero("ID del envío", minimo=1))
        opciones = TRANSICIONES_ENVIO.get(e.estado, ())
        if not opciones:
            ui.error(f"El envío está {e.estado}: es un estado final.")
            return
        nuevo = ui.pedir_opcion(f"Estado actual {e.estado}. Nuevo estado", list(opciones))
        liberados = self._srv.cambiar_estado(e.id_envio, nuevo)
        ui.ok(f"Envío {e.id_envio}: {e.estado} → {nuevo}")
        if liberados:
            ui.info(f"Contenedores liberados (vuelven a DISPONIBLE): {', '.join(map(str, liberados))}")

    def ruta_grafo(self, sesion: Sesion) -> None:
        id_envio = ui.pedir_entero("ID del envío", minimo=1)
        self._srv.obtener(id_envio, cliente_de(sesion))
        filas = self._srv.ruta_en_grafo(id_envio)
        ui.tabla(["#", "Desde", "Hasta", "Medio", "Operador", "Vehículo"],
                 [(f["orden"], f["desde"], f["hasta"], f["medio"], f["operador"], f["vehiculo"] or "—") for f in filas],
                 f"Ruta del envío {id_envio} leída desde Neo4j")
        if not filas:
            ui.info("Si recién lo cargaste, esperá unos segundos a que el sincronizador lo pase al grafo.")


    # ------------------------------------------------------------------ fase 9
    def modificar(self, sesion: Sesion) -> None:
        e = self._srv.obtener(ui.pedir_entero("ID del envío", minimo=1))
        ui.info(f"Hoy: prioridad {e.prioridad}, entrega estimada {ui.fecha(e.fecha_entrega_estimada)} (Enter = sin cambio)")
        prioridad = ui.pedir_opcion("Prioridad", list(PRIORIDADES), defecto=e.prioridad)
        fecha = ui.pedir_fecha_hora("Entrega estimada", defecto=e.fecha_entrega_estimada.strftime("%Y-%m-%d %H:%M"))
        self._srv.modificar_envio(e.id_envio, prioridad, fecha)
        ui.ok(f"Envío {e.id_envio} modificado")

    def agregar_contenedor(self, sesion: Sesion) -> None:
        id_envio = ui.pedir_entero("ID del envío", minimo=1)
        libres = self._contenedores.listar_contenedores("DISPONIBLE")
        ui.tabla(["ID", "Código", "Tipo", "Capacidad m³"],
                 [(c.id_contenedor, c.codigo_internacional, c.tipo, c.capacidad_m3) for c in libres[:20]],
                 "Contenedores disponibles")
        self._srv.agregar_contenedor(id_envio, ui.pedir_entero("ID del contenedor", minimo=1))
        ui.ok("Contenedor agregado (pasa a EN_USO; sus sensores viajan con él)")

    def quitar_contenedor(self, sesion: Sesion) -> None:
        e = self._srv.obtener(ui.pedir_entero("ID del envío", minimo=1))
        ui.info(f"Contenedores del envío: {', '.join(map(str, e.contenedores))}")
        self._srv.quitar_contenedor(e.id_envio, ui.pedir_entero("ID del contenedor a quitar", minimo=1))
        ui.ok("Contenedor quitado (vuelve a DISPONIBLE)")


__all__ = ["MenuEnvios", "ESTADOS_ENVIO", "ESTADOS_ENVIO_EN_CURSO", "operador_de"]
