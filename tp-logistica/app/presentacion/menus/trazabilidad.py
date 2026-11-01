"""Menú de trazabilidad: seguimiento de envíos, eventos logísticos e incidentes."""
from __future__ import annotations

import webbrowser

from app.dominio.modelos import TIPOS_EVENTO, TIPOS_INCIDENTE, TRANSICIONES_INCIDENTE, Sesion
from app.presentacion import componentes as ui
from app.presentacion.menus.base import MenuBase
from app.presentacion.menus.envios import cliente_de


class MenuTrazabilidad(MenuBase):
    TITULO = "Trazabilidad e incidentes"
    OPCIONES = [
        ("1", "Seguimiento completo de un envío", "seguimiento", 1),
        ("2", "Incidentes de un envío", "incidentes_envio", 1),
        ("3", "Registrar un evento logístico", "registrar_evento", 3),
        ("4", "Registrar un incidente", "registrar_incidente", 3),
        ("5", "Incidentes abiertos", "incidentes_abiertos", 2),
        ("6", "Cambiar estado de un incidente", "estado_incidente", 3),
        ("7", "Ranking de rutas por incidentes (MongoDB + caché Redis)", "ranking", 2),
        ("8", "Rutas problemáticas y ruta alternativa (Neo4j)", "rutas_neo4j", 2),
        ("9", "Eventos cerca de una ubicación (geoespacial)", "cerca", 2),
        # Fase 9
        ("10", "Recorrido de un contenedor (Neo4j + mapa)", "recorrido_contenedor", 2),
        ("11", "Red de relaciones de un cliente (Neo4j)", "red_cliente", 2),
        ("12", "Ver el recorrido GPS de un envío en un mapa", "mapa_envio", 1),
    ]

    def __init__(self, ejecutar, sesion_actual, servicio_trazabilidad):
        super().__init__(ejecutar, sesion_actual)
        self._srv = servicio_trazabilidad

    def seguimiento(self, sesion: Sesion) -> None:
        s = self._srv.seguimiento(ui.pedir_entero("ID del envío", minimo=1), cliente_de(sesion))
        e = s["envio"]
        ui.tabla(["Dato", "Valor"], [
            ("Envío", f"{e.id_envio}: {e.origen} → {e.destino} ({e.cliente})"),
            ("Estado (PostgreSQL)", e.estado),
            ("Ruta (PostgreSQL)", " → ".join([e.origen] + [t.destino for t in e.tramos]) or "sin tramos"),
            ("Entrega estimada", ui.fecha(e.fecha_entrega_estimada)),
            ("Última posición (Cassandra)",
             f"{s['posicion']['latitud']:.4f}, {s['posicion']['longitud']:.4f} a las {ui.fecha(s['posicion']['fecha_hora'])}"
             if s["posicion"] else "—"),
            ("Incidentes (MongoDB)", "no disponible" if any("MongoDB" in m for m in s["no_disponible"]) else
             f"{len(s['incidentes'])} ({sum(1 for i in s['incidentes'] if i.estado in ('ABIERTO', 'EN_ANALISIS'))}"
             " abiertos)"),
        ], f"Seguimiento del envío {e.id_envio}")
        # Línea de tiempo: eventos e incidentes juntos, ordenados por fecha
        linea = [(ev.fecha_hora, ev.tipo, ev.codigo_ubicacion, ev.descripcion or "",
                  ev.nombre_usuario or "sistema") for ev in s["eventos"]]
        linea += [(i.fecha, f"INCIDENTE {i.tipo}", f"tramo {i.id_tramo}" if i.id_tramo else "—",
                   f"severidad {i.severidad}, {i.estado}: {i.descripcion}", "") for i in s["incidentes"]]
        ui.tabla(["Fecha (UTC)", "Qué pasó", "Dónde", "Detalle", "Cargado por"],
                 [(ui.fecha(f), *resto) for f, *resto in sorted(linea, key=lambda x: x[0])],
                 "Línea de tiempo (MongoDB: eventos_logisticos + incidentes)")
        for motor in s["no_disponible"]:
            ui.error(f"No se pudo consultar {motor}: se muestra el resto")

    def incidentes_envio(self, sesion: Sesion) -> None:
        lista = self._srv.incidentes_de_envio(ui.pedir_entero("ID del envío", minimo=1), cliente_de(sesion))
        self._tabla_incidentes(lista, f"{len(lista)} incidentes del envío")

    def _tabla_incidentes(self, lista, titulo) -> None:
        ui.tabla(["ID", "Fecha (UTC)", "Envío", "Tipo", "Sev.", "Estado", "Ruta", "Tramo", "Descripción"],
                 [(i.id_incidente[-8:], ui.fecha(i.fecha), i.id_envio, i.tipo, i.severidad, i.estado, i.ruta,
                   i.id_tramo or "—", i.descripcion) for i in lista], titulo)

    def registrar_evento(self, sesion: Sesion) -> None:
        id_envio = ui.pedir_entero("ID del envío", minimo=1)
        tipo = ui.pedir_opcion("Tipo", list(TIPOS_EVENTO), defecto="OTRO")
        codigo = ui.pedir("Ubicación (código)").upper()
        descripcion = ui.pedir("Qué pasó")
        self._srv.registrar_evento(id_envio, tipo, codigo, descripcion, sesion)
        ui.ok("Evento registrado en MongoDB (aparece en el seguimiento)")

    def registrar_incidente(self, sesion: Sesion) -> None:
        id_envio = ui.pedir_entero("ID del envío", minimo=1)
        tipo = ui.pedir_opcion("Tipo", list(TIPOS_INCIDENTE))
        severidad = ui.pedir_entero("Severidad (1 leve a 5 grave)", minimo=1, maximo=5)
        descripcion = ui.pedir("Descripción")
        id_tramo = ui.pedir_entero("ID del tramo (vacío = el que está en viaje)", minimo=1, obligatorio=False)
        id_incidente = self._srv.registrar_incidente(id_envio, tipo, severidad, descripcion, sesion, id_tramo)
        ui.ok(f"Incidente {id_incidente[-8:]} registrado en MongoDB; el sincronizador lo suma a la ruta en Neo4j")

    def incidentes_abiertos(self, sesion: Sesion) -> None:
        lista = self._srv.incidentes()
        self._tabla_incidentes(lista, f"{len(lista)} incidentes ABIERTOS o EN_ANALISIS, los más graves primero")
        if lista:
            ui.info("Para cambiar el estado alcanza con los 8 caracteres de la columna ID.")

    def estado_incidente(self, sesion: Sesion) -> None:
        corto = ui.pedir("ID del incidente (8 caracteres o completo)").lower()
        candidatos = [i for i in self._srv.incidentes(["ABIERTO", "EN_ANALISIS", "RESUELTO"])
                      if i.id_incidente.endswith(corto)]
        if len(candidatos) != 1:
            ui.error("No hay un único incidente (no cerrado) con ese ID")
            return
        i = candidatos[0]
        opciones = list(TRANSICIONES_INCIDENTE[i.estado])
        nuevo = ui.pedir_opcion(f"Estado actual {i.estado}. Nuevo estado", opciones)
        self._srv.cambiar_estado_incidente(i.id_incidente, nuevo, sesion)
        ui.ok(f"Incidente {i.tipo}: {i.estado} → {nuevo} (queda en su historial)")

    def ranking(self, sesion: Sesion) -> None:
        filas, de_cache = self._srv.ranking_rutas()
        ui.tabla(["Ruta", "Incidentes", "Abiertos", "Severidad prom.", "Tipos"],
                 [(f["ruta"], f["incidentes"], f["abiertos"], f["severidad_prom"], ", ".join(sorted(f["tipos"])))
                  for f in filas],
                 "Ranking de rutas " + ("(desde la caché de Redis)" if de_cache else "(calculado en MongoDB)"))

    def rutas_neo4j(self, sesion: Sesion) -> None:
        ui.tabla(["Desde", "Hasta", "Medio", "Incidentes", "Tramos", "Tasa", "Demora prom. (h)"],
                 [(f["desde"], f["hasta"], f["medio"], f["incidentes"], f["envios"], f["tasa"], f["demora_h"])
                  for f in self._srv.rutas_problematicas()], "Rutas con más incidentes (Neo4j, relación RUTA)")
        if not ui.confirmar("¿Buscar una ruta alternativa que evite las rutas problemáticas?"):
            return
        desde = ui.pedir("Desde (código)").upper()
        hasta = ui.pedir("Hasta (código)").upper()
        tasa = ui.pedir_decimal("Tasa máxima de incidentes por tramo", minimo=0, defecto="0.2")
        r = self._srv.ruta_alternativa(desde, hasta, tasa)
        if r is None:
            ui.error("No hay un camino entre esas ubicaciones con rutas por debajo de esa tasa")
        else:
            ui.ok(f"Ruta: {' → '.join(r['escalas'])} ({r['tramos']} tramos: {', '.join(r['medios'])})")

    def cerca(self, sesion: Sesion) -> None:
        codigo = ui.pedir("Ubicación de referencia (código)").upper()
        km = ui.pedir_decimal("Radio en km", minimo=1, defecto="500")
        lista = self._srv.eventos_cerca(codigo, km)
        ui.tabla(["Fecha (UTC)", "Envío", "Tipo", "Ubicación", "Detalle"],
                 [(ui.fecha(e.fecha_hora), e.id_envio, e.tipo, e.codigo_ubicacion, e.descripcion or "")
                  for e in lista], f"{len(lista)} eventos a menos de {km:g} km de {codigo}, del más cercano al más lejano")

    # ------------------------------------------------------------------ fase 9
    @staticmethod
    def _abrir(ruta) -> None:
        ui.ok(f"Mapa guardado en {ruta}")
        if ui.confirmar("¿Abrirlo en el navegador?", defecto=True):
            webbrowser.open(ruta.resolve().as_uri())

    def recorrido_contenedor(self, sesion: Sesion) -> None:
        id_contenedor = ui.pedir_entero("ID del contenedor", minimo=1)
        viajes = self._srv.historial_contenedor(id_contenedor)
        ui.tabla(["Envío", "Cliente", "Recorrido", "Estado", "Subió", "Bajó"],
                 [(v["id_envio"], v["cliente"] or "—", " → ".join([v["origen"] or "?"] + v["escalas"]), v["estado"],
                   ui.fecha(v["desde"]), ui.fecha(v["hasta"]) if v["hasta"] else "sigue a bordo") for v in viajes],
                 f"Contenedor {id_contenedor}: {len(viajes)} viajes (Neo4j, relación VIAJA_EN)")
        if viajes and ui.confirmar("¿Ver sus posiciones GPS en un mapa? (Cassandra)", defecto=True):
            self._abrir(self._srv.mapa_contenedor(id_contenedor))

    def red_cliente(self, sesion: Sesion) -> None:
        id_cliente = ui.pedir_entero("ID del cliente", minimo=1)
        filas = self._srv.red_cliente(id_cliente)
        ui.tabla(["Operador", "Envíos del cliente", "Otros clientes que trabajan con ese operador"],
                 [(f["operador"], f["envios"], ", ".join(f["otros_clientes"]) or "—") for f in filas],
                 f"Red de relaciones del cliente {id_cliente} (Neo4j: Cliente-SOLICITO-Envio-...-OPERADO_POR-Operador)")

    def mapa_envio(self, sesion: Sesion) -> None:
        self._abrir(self._srv.mapa_envio(ui.pedir_entero("ID del envío", minimo=1), cliente_de(sesion)))
