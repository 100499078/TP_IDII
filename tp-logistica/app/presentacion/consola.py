"""Aplicación de consola: inicio de sesión y menú principal.

La consola solo muestra y pide datos; toda regla de negocio está en los
servicios. Cada opción del menú pasa por `_ejecutar`, que:
  1. valida (y renueva) la sesión en Redis antes de la operación,
  2. atrapa los errores conocidos y muestra su mensaje,
  3. registra en el log cualquier error inesperado sin mostrar un traceback.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from app import __version__
from app.dominio.modelos import Sesion
from app.excepciones import ErrorAplicacion, ErrorAutenticacion
from app.presentacion import componentes as ui
from app.servicios.autenticacion import ServicioAutenticacion
from app.servicios.estado import ServicioEstado

log = logging.getLogger(__name__)

INTENTOS_MAXIMOS = 3


class SalirDelMenu(Exception):
    """Señal interna para volver a la pantalla de inicio de sesión."""


@dataclass
class ServiciosOperativos:
    """Servicios que usan los menús (los arma app/__main__.py)."""
    actores: Any
    usuarios: Any
    contenedores: Any
    catalogos: Any
    envios: Any
    sincronizacion: Any
    hilo_sincronizacion: Any = None
    telemetria: Any = None           # fase 3
    simulador: Any = None            # fase 3
    trazabilidad: Any = None         # fase 4
    mensajeria: Any = None           # fase 5
    comercial: Any = None            # fase 6
    fase7: Any = None                # fase 7: procesos, reportes, riesgo y cierres
    verificacion: Any = None         # fase 8: consistencia entre motores


class AplicacionConsola:
    def __init__(self, autenticacion: ServicioAutenticacion, estado: ServicioEstado,
                 servicios: ServiciosOperativos | None = None):
        self._auth = autenticacion
        self._estado = estado
        self._sesion: Sesion | None = None
        # (clave, texto, nivel mínimo, menú). Cada menú solo aparece si el usuario tiene el nivel.
        self._submenus: list[tuple[str, str, int, Any]] = self._armar_menus(servicios) if servicios else []
        self._telemetria = servicios.telemetria if servicios else None
        self._mensajeria = servicios.mensajeria if servicios else None

    def _armar_menus(self, s: ServiciosOperativos) -> list[tuple[str, str, int, Any]]:
        from app.presentacion.menus.actores import MenuActores
        from app.presentacion.menus.catalogos import MenuCatalogos
        from app.presentacion.menus.comercial import MenuComercial
        from app.presentacion.menus.contenedores import MenuContenedores
        from app.presentacion.menus.envios import MenuEnvios
        from app.presentacion.menus.iot import MenuIoT
        from app.presentacion.menus.mensajeria import MenuMensajeria
        from app.presentacion.menus.procesos import MenuProcesos
        from app.presentacion.menus.sistema import MenuSistema
        from app.presentacion.menus.trazabilidad import MenuTrazabilidad
        from app.presentacion.menus.usuarios import MenuUsuarios
        args = (self._ejecutar, lambda: self._sesion)
        return [
            ("1", "Envíos y tramos", 1, MenuEnvios(*args, s.envios, s.contenedores, s.catalogos)),
            ("2", "Clientes, operadores y proveedores", 2, MenuActores(*args, s.actores)),
            ("3", "Contenedores y sensores", 2, MenuContenedores(*args, s.contenedores)),
            ("4", "Ubicaciones, vehículos y tarifas", 1, MenuCatalogos(*args, s.catalogos)),
            ("5", "Usuarios y roles", 5, MenuUsuarios(*args, s.usuarios)),
            ("6", "Sistema y sincronización", 4, MenuSistema(*args, s.sincronizacion, s.hilo_sincronizacion,
                                                                   s.verificacion)),
            ("7", "Sensores y telemetría (IoT)", 1, MenuIoT(*args, s.telemetria, s.envios, s.simulador)),
            ("8", "Trazabilidad e incidentes", 1, MenuTrazabilidad(*args, s.trazabilidad)),
            ("9", "Mensajería", 1, MenuMensajeria(*args, s.mensajeria)),
            ("10", "Facturación y cuenta corriente", 1, MenuComercial(*args, s.comercial)),
            ("11", "Reportes, riesgo y procesos", 1, MenuProcesos(*args, s.fase7)),
        ]

    # ------------------------------------------------------------------ ciclo principal
    def ejecutar(self) -> None:
        ui.titulo(f"Plataforma de Logística Internacional  v{__version__}",
                  "TP Persistencia Políglota — Ingeniería de Datos II")
        self._mostrar_estado(resumido=True)
        while True:
            if not self._iniciar_sesion():
                ui.info("Hasta luego.")
                return
            try:
                self._menu_principal()
            except SalirDelMenu:
                continue

    # ------------------------------------------------------------------ sesión
    def _iniciar_sesion(self) -> bool:
        """Devuelve True si inició sesión; False si el usuario eligió salir."""
        for intento in range(1, INTENTOS_MAXIMOS + 1):
            ui.consola.print("\n[bold]Iniciar sesión[/bold] [dim](dejá el email vacío para salir)[/dim]")
            email = ui.pedir("Email", obligatorio=False)
            if not email:
                return False
            password = ui.pedir("Contraseña", oculto=True)
            try:
                self._sesion = self._auth.iniciar_sesion(email, password)
                ui.ok(f"Bienvenida/o, {self._sesion.nombre} "
                      f"(roles: {', '.join(self._sesion.roles) or 'ninguno'})")
                return True
            except ErrorAplicacion as exc:
                ui.error(str(exc))
            except Exception:  # noqa: BLE001
                log.exception("Error inesperado al iniciar sesión")
                ui.error("Ocurrió un error inesperado. El detalle quedó en logs/errores.log")
            if intento < INTENTOS_MAXIMOS:
                ui.info(f"Intento {intento} de {INTENTOS_MAXIMOS}.")
        ui.error("Demasiados intentos fallidos.")
        return False

    def _cerrar_sesion(self) -> None:
        if self._sesion:
            try:
                self._auth.cerrar_sesion(self._sesion)
                ui.ok("Sesión cerrada.")
            except ErrorAplicacion as exc:
                ui.error(str(exc))
        self._sesion = None
        raise SalirDelMenu

    # ------------------------------------------------------------------ ejecución segura
    def _ejecutar(self, accion: Callable[[Sesion], None], nivel_minimo: int = 1) -> None:
        try:
            sesion = self._auth.validar(self._sesion.id_sesion)
            self._sesion = sesion
            self._auth.exigir_nivel(sesion, nivel_minimo)
            accion(sesion)
        except SalirDelMenu:          # viene de un submenú cuya sesión venció: se propaga
            raise
        except ErrorAutenticacion as exc:
            ui.error(str(exc))
            self._sesion = None
            raise SalirDelMenu
        except ErrorAplicacion as exc:
            ui.error(str(exc))
        except KeyboardInterrupt:
            ui.info("Operación cancelada.")
        except Exception:  # noqa: BLE001
            log.exception("Error inesperado en %s", getattr(accion, "__name__", accion))
            ui.error("Ocurrió un error inesperado. El detalle quedó en logs/errores.log")

    # ------------------------------------------------------------------ menús
    def _menu_principal(self) -> None:
        while True:
            nivel = self._sesion.nivel_permisos
            if nivel >= 3:
                self._mostrar_alertas_nuevas()
            self._mostrar_mensajes_sin_leer()
            visibles = [(c, t, n, m) for c, t, n, m in self._submenus if nivel >= n]
            opcion = ui.menu(f"Menú principal — {self._sesion.nombre}",
                             [(c, t) for c, t, _, _ in visibles] + [
                                 ("E", "Estado de las bases de datos"),
                                 ("M", "Mi sesión"),
                                 ("0", "Cerrar sesión"),
                             ])
            if opcion == "E":
                self._ejecutar(lambda s: self._mostrar_estado())
            elif opcion == "M":
                self._ejecutar(self._mostrar_sesion)
            elif opcion == "0":
                self._cerrar_sesion()
            else:
                _, _, nivel_minimo, submenu = next(x for x in visibles if x[0] == opcion)
                self._ejecutar(submenu.mostrar, nivel_minimo)

    # ------------------------------------------------------------------ avisos
    def _mostrar_alertas_nuevas(self) -> None:
        """Muestra las alertas que llegaron por alertas:stream desde la última vez (Redis Streams).
        Cada consola es un consumidor del grupo "notificadores": un aviso le llega a una sola."""
        if self._telemetria is None:
            return
        try:
            avisos = self._telemetria.alertas_nuevas(f"consola-{self._sesion.id_usuario}")
        except Exception as exc:  # noqa: BLE001   (sin Redis no hay avisos, pero el menú sigue)
            log.debug("No se pudieron leer las alertas nuevas: %s", exc)
            return
        for a in avisos:
            ui.error(f"⚠ ALERTA {a.get('tipo')}: {a.get('mensaje')}")
        if avisos:
            ui.info("Detalle en: Sensores y telemetría → Alertas abiertas")

    def _mostrar_mensajes_sin_leer(self) -> None:
        if self._mensajeria is None:
            return
        try:
            total = self._mensajeria.total_no_leidos(self._sesion)
        except Exception as exc:  # noqa: BLE001   (sin MongoDB ni Redis no hay aviso, pero el menú sigue)
            log.debug("No se pudieron contar los mensajes sin leer: %s", exc)
            return
        if total:
            ui.consola.print(f"[bold yellow]✉ Tenés {total} mensaje{'s' if total > 1 else ''} sin leer "
                             "(9 Mensajería)[/bold yellow]")

    # ------------------------------------------------------------------ pantallas
    def _mostrar_estado(self, resumido: bool = False) -> None:
        estados = self._estado.verificar()
        if resumido:
            caidos = [e.motor for e in estados if not e.ok]
            if caidos:
                ui.error("Sin conexión con: " + ", ".join(caidos)
                         + ". Las opciones que los usan van a fallar hasta que estén levantados.")
            else:
                ui.ok("Conectado a los 5 motores.")
            return
        ui.tabla(["Motor", "Estado", "Detalle", "Latencia"],
                 [(e.motor, "[green]OK[/green]" if e.ok else "[red]CAÍDO[/red]", e.detalle,
                   f"{e.milisegundos:.0f} ms" if e.milisegundos is not None else "—") for e in estados],
                 "Estado de las bases de datos")

    def _mostrar_sesion(self, sesion: Sesion) -> None:
        restantes = self._auth.segundos_restantes(sesion)
        ui.tabla(["Dato", "Valor"], [
            ("Sesión", sesion.id_sesion),
            ("Usuario", f"{sesion.nombre} <{sesion.email}>"),
            ("Roles", ", ".join(sesion.roles)),
            ("Nivel de permisos", sesion.nivel_permisos),
            ("Inicio (UTC)", sesion.fecha_inicio.strftime("%Y-%m-%d %H:%M:%S")),
            ("Vence en", f"{restantes // 60} min {restantes % 60} s (se renueva con cada operación)"),
        ], "Mi sesión (guardada en Redis)")
