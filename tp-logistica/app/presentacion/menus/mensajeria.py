"""Menú de mensajería: bandeja, conversaciones privadas y grupos de trabajo."""
from __future__ import annotations

from rich.markup import escape

from app.dominio.modelos import Sesion
from app.presentacion import componentes as ui
from app.presentacion.menus.base import MenuBase


class MenuMensajeria(MenuBase):
    TITULO = "Mensajería"
    OPCIONES = [
        ("1", "Bandeja de entrada", "bandeja", 1),
        ("2", "Abrir una conversación", "abrir", 1),
        ("3", "Escribirle a una persona", "nuevo_privado", 1),
        ("4", "Mis grupos de trabajo", "grupos", 1),
        ("5", "Crear un grupo de trabajo", "crear_grupo", 3),
        ("6", "Agregar integrante a un grupo", "agregar", 3),
        ("7", "Quitar integrante de un grupo", "quitar", 3),
    ]

    def __init__(self, ejecutar, sesion_actual, servicio_mensajeria):
        super().__init__(ejecutar, sesion_actual)
        self._srv = servicio_mensajeria

    # ------------------------------------------------------------------ bandeja y chat
    def _mostrar_bandeja(self, sesion: Sesion) -> list[dict]:
        filas = self._srv.bandeja(sesion)
        ui.tabla(["#", "Conversación", "Último mensaje", "Fecha (UTC)", "Sin leer"],
                 [(n, escape(f["conversacion"].titulo), escape((f["conversacion"].ultimo_resumen or "—")[:50]),
                   ui.fecha(f["conversacion"].ultimo_fecha), f["no_leidos"] or "")
                  for n, f in enumerate(filas, 1)], "Bandeja (MongoDB; contadores sin leer en Redis)")
        return filas

    def bandeja(self, sesion: Sesion) -> None:
        self._mostrar_bandeja(sesion)

    def abrir(self, sesion: Sesion) -> None:
        filas = self._mostrar_bandeja(sesion)
        if not filas:
            return
        n = ui.pedir_entero("Número de conversación", minimo=1, maximo=len(filas))
        c = filas[n - 1]["conversacion"]
        self._chat(sesion, c.id_conversacion, c.titulo)

    def _chat(self, sesion: Sesion, id_conversacion: str, titulo: str) -> None:
        """Muestra los últimos mensajes y deja responder. Enter vacío vuelve; '/a' actualiza."""
        while True:
            mensajes = self._srv.abrir(id_conversacion, sesion)
            ui.titulo(titulo, "Enter vacío = volver · /a = actualizar")
            for m in mensajes:
                quien = "yo" if m.id_remitente == sesion.id_usuario else m.nombre_remitente
                if m.tipo == "SISTEMA":
                    ui.info(f"  {ui.fecha(m.fecha_hora)}  · {escape(m.contenido)}")
                else:
                    # escape(): lo que escribe un usuario no se interpreta como formato de rich ("[rojo]...")
                    ui.consola.print(f"  [dim]{ui.fecha(m.fecha_hora)}[/dim]  [bold]{escape(quien)}:[/bold] "
                                     f"{escape(m.contenido)}", highlight=False)
            if not mensajes:
                ui.info("Todavía no hay mensajes.")
            texto = ui.pedir("Mensaje", obligatorio=False)
            if not texto:
                return
            if texto.strip().lower() == "/a":
                continue
            self._srv.enviar(id_conversacion, texto, sesion)

    def nuevo_privado(self, sesion: Sesion) -> None:
        contactos = self._srv.contactos(sesion)
        ui.tabla(["ID", "Nombre", "Email", "Estado"],
                 [(c["usuario"].id_usuario, c["usuario"].nombre_completo, c["usuario"].email,
                   "[green]en línea[/green]" if c["en_linea"] else "desconectado") for c in contactos],
                 "Contactos (en línea = tiene una sesión activa en Redis)")
        if not contactos:
            return
        id_destino = ui.pedir_entero("ID de la persona", minimo=1)
        c = self._srv.conversacion_privada(sesion, id_destino)
        nombre = next((x["usuario"].nombre_completo for x in contactos if x["usuario"].id_usuario == id_destino),
                      f"Usuario {id_destino}")
        self._chat(sesion, c.id_conversacion, nombre)

    # ------------------------------------------------------------------ grupos
    def _mostrar_grupos(self, sesion: Sesion):
        grupos = self._srv.grupos(sesion)
        ui.tabla(["#", "Grupo", "Integrantes", "Mi rol", "Descripción"],
                 [(n, g.nombre, ", ".join(i.nombre for i in g.integrantes),
                   "ADMIN" if g.es_admin(sesion.id_usuario) else "MIEMBRO", g.descripcion or "")
                  for n, g in enumerate(grupos, 1)], "Mis grupos de trabajo (MongoDB, integrantes embebidos)")
        return grupos

    def _elegir_grupo(self, sesion: Sesion):
        grupos = self._mostrar_grupos(sesion)
        if not grupos:
            return None
        return grupos[ui.pedir_entero("Número de grupo", minimo=1, maximo=len(grupos)) - 1]

    def grupos(self, sesion: Sesion) -> None:
        self._mostrar_grupos(sesion)

    def crear_grupo(self, sesion: Sesion) -> None:
        nombre = ui.pedir("Nombre del grupo")
        descripcion = ui.pedir("Descripción", obligatorio=False)
        ui.tabla(["ID", "Nombre", "Email"],
                 [(c["usuario"].id_usuario, c["usuario"].nombre_completo, c["usuario"].email)
                  for c in self._srv.contactos(sesion, solo_internos=True)], "Personal que se puede agregar")
        ids = ui.pedir_lista_enteros("IDs de los integrantes")
        self._srv.crear_grupo(sesion, nombre, descripcion or "", ids)
        ui.ok(f"Grupo {nombre} creado; ya tiene su conversación grupal en la bandeja")

    def agregar(self, sesion: Sesion) -> None:
        g = self._elegir_grupo(sesion)
        if g:
            self._srv.agregar_integrante(g.id_grupo, ui.pedir_entero("ID del usuario a agregar", minimo=1), sesion)
            ui.ok("Integrante agregado (y avisado en la conversación del grupo)")

    def quitar(self, sesion: Sesion) -> None:
        g = self._elegir_grupo(sesion)
        if g:
            self._srv.quitar_integrante(g.id_grupo, ui.pedir_entero("ID del usuario a quitar", minimo=1), sesion)
            ui.ok("Integrante quitado")
