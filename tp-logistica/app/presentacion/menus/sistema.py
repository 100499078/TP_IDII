"""Menú de sistema: estado de la sincronización PostgreSQL → Redis → Neo4j y, desde la fase 8,
el verificador de consistencia entre motores."""
from __future__ import annotations

from app.dominio.modelos import Sesion
from app.servicios.verificacion import ADVERTENCIA, ERROR, OK
from app.presentacion import componentes as ui
from app.presentacion.menus.base import MenuBase


class MenuSistema(MenuBase):
    TITULO = "Sistema y sincronización"
    OPCIONES = [
        ("1", "Estado de la sincronización", "estado", 4),
        ("2", "Sincronizar ahora", "sincronizar", 4),
        ("3", "Reconstruir el grafo desde PostgreSQL", "reconstruir", 5),
        ("4", "Verificar la consistencia entre motores", "verificar", 4),
    ]

    def __init__(self, ejecutar, sesion_actual, servicio_sincronizacion, hilo=None, servicio_verificacion=None):
        super().__init__(ejecutar, sesion_actual)
        self._srv = servicio_sincronizacion
        self._hilo = hilo
        self._verificacion = servicio_verificacion

    def estado(self, sesion: Sesion) -> None:
        d = self._srv.estado()
        hilo = "activo" if self._hilo and self._hilo.is_alive() else "detenido"
        ui.tabla(["Paso", "Valor"], [
            ("Hilo sincronizador", hilo),
            ("PostgreSQL: eventos en outbox sin publicar", d["outbox_pendientes"]),
            ("Redis: mensajes en sync:stream", d["en_stream"]),
            ("Redis: entregados sin confirmar (XPENDING)", d["sin_confirmar"]),
            ("Redis: eventos fallidos (sync:fallidos)", d["fallidos"]),
            *[(f"Sin confirmar por {nombre}", n) for nombre, n in d.get("proyecciones", {}).items()],
        ], "PostgreSQL → Redis → Neo4j y MongoDB")
        grafo = d["grafo"]
        if isinstance(grafo, dict):
            ui.tabla(["Etiqueta", "Nodos"], sorted(grafo.items()), "Contenido del grafo")
        else:
            ui.error(grafo)

    def sincronizar(self, sesion: Sesion) -> None:
        if self._hilo and self._hilo.is_alive():
            self._hilo.despertar()
            ui.ok("Se le pidió al sincronizador que procese ya los eventos pendientes")
        else:
            publicados, aplicados = self._srv.ciclo()
            ui.ok(f"{publicados} eventos publicados en Redis, {aplicados} aplicados en Neo4j y MongoDB")

    def reconstruir(self, sesion: Sesion) -> None:
        if ui.confirmar("Se borra el grafo y se vuelve a armar con los datos de PostgreSQL. ¿Continuar?"):
            ui.ok(f"Grafo reconstruido con {self._srv.reconstruir_grafo()} eventos")

    def verificar(self, sesion: Sesion) -> None:
        mostrar_controles(self._verificacion.verificar())


COLORES = {OK: "green", ADVERTENCIA: "yellow", ERROR: "red"}


def mostrar_controles(controles) -> None:
    """Tabla del verificador (la usan este menú y herramientas/verificar.py)."""
    filas = []
    for c in controles:
        color = COLORES.get(c.estado, "dim")
        ejemplos = f" [dim](ej.: {', '.join(map(str, c.ejemplos))})[/dim]" if c.ejemplos else ""
        filas.append((f"[{color}]{c.estado}[/{color}]", c.nombre, c.motores, c.detalle + ejemplos))
    ui.tabla(["Estado", "Control", "Motores", "Detalle"], filas, "Consistencia entre motores")
    resumen = {e: sum(1 for c in controles if c.estado == e) for e in (OK, ADVERTENCIA, ERROR)}
    ui.info(f"{resumen[OK]} OK, {resumen[ADVERTENCIA]} con advertencias, {resumen[ERROR]} con errores")
