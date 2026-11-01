"""Menú de clientes, operadores logísticos y proveedores (ABM)."""
from __future__ import annotations

from app.dominio.modelos import MEDIOS, TIPOS_ACTOR, Actor, Sesion
from app.dominio.validaciones import validar_cuit, validar_email, validar_pais
from app.presentacion import componentes as ui
from app.presentacion.menus.base import MenuBase


class MenuActores(MenuBase):
    TITULO = "Clientes, operadores y proveedores"
    OPCIONES = [
        ("1", "Listar / buscar", "listar", 2),
        ("2", "Ver detalle", "ver", 2),
        ("3", "Alta", "alta", 3),
        ("4", "Modificar", "modificar", 3),
        ("5", "Baja (lógica)", "baja", 4),
        ("6", "Reactivar", "reactivar", 4),
    ]

    def __init__(self, ejecutar, sesion_actual, servicio_actores):
        super().__init__(ejecutar, sesion_actual)
        self._srv = servicio_actores

    def listar(self, sesion: Sesion) -> None:
        tipo = ui.pedir_opcion("Tipo", ["TODOS", *TIPOS_ACTOR], defecto="TODOS")
        texto = ui.pedir("Buscar por razón social o CUIT (vacío = todos)", obligatorio=False)
        actores = self._srv.listar(None if tipo == "TODOS" else tipo, texto=texto or None)
        ui.tabla(["ID", "Tipo", "Razón social", "CUIT", "País", "Email", "Estado"],
                 [(a.id_actor, a.tipo, a.razon_social, a.cuit, a.pais, a.email, a.estado) for a in actores],
                 f"{len(actores)} actores")

    def ver(self, sesion: Sesion) -> None:
        a = self._srv.obtener(ui.pedir_entero("ID del actor", minimo=1))
        filas = [("Tipo", a.tipo), ("Razón social", a.razon_social), ("CUIT", a.cuit), ("Email", a.email),
                 ("Teléfono", a.telefono), ("Dirección", a.direccion), ("País", a.pais), ("Estado", a.estado)]
        if a.tipo == "CLIENTE":
            filas += [("Límite de crédito", ui.dinero(a.limite_credito)),
                      ("Condición de pago", f"{a.condicion_pago_dias} días")]
        elif a.tipo == "OPERADOR":
            filas.append(("Medios de transporte", ", ".join(a.medios)))
        else:
            filas.append(("Rubro", a.rubro))
        ui.tabla(["Dato", "Valor"], filas, f"Actor {a.id_actor}")

    def _pedir_datos(self, a: Actor) -> Actor:
        """Pide los datos comunes y los del subtipo; con un actor existente propone sus valores."""
        a.razon_social = ui.pedir("Razón social", defecto=a.razon_social or None)
        a.email = ui.pedir("Email", defecto=a.email or None, validar=validar_email)
        a.telefono = ui.pedir("Teléfono", obligatorio=False, defecto=a.telefono or "") or None
        a.direccion = ui.pedir("Dirección", obligatorio=False, defecto=a.direccion or "") or None
        a.pais = ui.pedir("País (ISO, ej. AR)", defecto=a.pais or None, validar=lambda v: validar_pais(v.upper())).upper()
        if a.tipo == "CLIENTE":
            a.limite_credito = ui.pedir_decimal("Límite de crédito", minimo=0,
                                                defecto=str(a.limite_credito if a.limite_credito is not None else 0))
            a.condicion_pago_dias = ui.pedir_entero("Condición de pago (días)", minimo=0, maximo=180)
        elif a.tipo == "OPERADOR":
            texto = ui.pedir(f"Medios de transporte, separados por coma ({', '.join(MEDIOS)})",
                             defecto=",".join(a.medios) or None)
            a.medios = [m.strip().upper() for m in texto.split(",") if m.strip()]
        else:
            a.rubro = ui.pedir("Rubro", defecto=a.rubro or None)
        return a

    def alta(self, sesion: Sesion) -> None:
        tipo = ui.pedir_opcion("Tipo", list(TIPOS_ACTOR))
        cuit = ui.pedir("CUIT (30-12345678-9)", validar=validar_cuit)
        actor = self._pedir_datos(Actor(None, tipo, "", cuit, "", ""))
        ui.ok(f"{tipo.capitalize()} creado con ID {self._srv.crear(actor)}")

    def modificar(self, sesion: Sesion) -> None:
        actor = self._srv.obtener(ui.pedir_entero("ID del actor", minimo=1))
        ui.info(f"Modificando {actor.razon_social} ({actor.tipo}). Enter conserva el valor actual.")
        self._srv.modificar(self._pedir_datos(actor))
        ui.ok("Datos actualizados")

    def baja(self, sesion: Sesion) -> None:
        actor = self._srv.obtener(ui.pedir_entero("ID del actor", minimo=1))
        if ui.confirmar(f"¿Dar de baja a {actor.razon_social}? Sus datos e historial se conservan"):
            self._srv.dar_de_baja(actor.id_actor)
            ui.ok("Actor dado de baja (estado INACTIVO)")

    def reactivar(self, sesion: Sesion) -> None:
        self._srv.reactivar(ui.pedir_entero("ID del actor", minimo=1))
        ui.ok("Actor reactivado")
