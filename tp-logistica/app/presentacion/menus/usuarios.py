"""Menú de usuarios y roles (solo administradores, nivel 5)."""
from __future__ import annotations

from app.dominio.modelos import Sesion
from app.dominio.validaciones import validar_email
from app.presentacion import componentes as ui
from app.presentacion.menus.base import MenuBase
from app.servicios.usuarios import validar_password


class MenuUsuarios(MenuBase):
    TITULO = "Usuarios y roles"
    OPCIONES = [
        ("1", "Listar usuarios", "listar", 5),
        ("2", "Ver roles", "roles", 5),
        ("3", "Alta de usuario", "alta", 5),
        ("4", "Cambiar roles", "cambiar_roles", 5),
        ("5", "Bloquear usuario", "bloquear", 5),
        ("6", "Desbloquear usuario", "desbloquear", 5),
        ("7", "Cambiar contraseña", "cambiar_password", 5),
        # Fase 9: ABM de roles y de sesiones activas
        ("8", "Alta de rol", "alta_rol", 5),
        ("9", "Modificar rol", "modificar_rol", 5),
        ("10", "Baja de rol", "baja_rol", 5),
        ("11", "Sesiones activas", "sesiones", 5),
        ("12", "Cerrar una sesión", "cerrar_sesion", 5),
        ("13", "Cerrar todas las sesiones de un usuario", "cerrar_sesiones_usuario", 5),
    ]

    def __init__(self, ejecutar, sesion_actual, servicio_usuarios):
        super().__init__(ejecutar, sesion_actual)
        self._srv = servicio_usuarios
        self._ultimas: list[dict] = []

    def listar(self, sesion: Sesion) -> None:
        lista = self._srv.listar()
        ui.tabla(["ID", "Nombre", "Email", "Roles", "Nivel", "Cliente", "Estado"],
                 [(u.id_usuario, u.nombre_completo, u.email, ", ".join(r.nombre for r in u.roles),
                   u.nivel_permisos, u.id_actor or "—", u.estado) for u in lista], f"{len(lista)} usuarios")

    def roles(self, sesion: Sesion) -> None:
        ui.tabla(["Rol", "Nivel", "Descripción"],
                 [(r.nombre, r.nivel_permisos, r.descripcion) for r in self._srv.roles()], "Roles")

    def _pedir_roles(self) -> list[str]:
        nombres = [r.nombre for r in self._srv.roles()]
        texto = ui.pedir(f"Roles separados por coma ({', '.join(nombres)})")
        return [r.strip().upper() for r in texto.split(",") if r.strip()]

    def alta(self, sesion: Sesion) -> None:
        nombre = ui.pedir("Nombre")
        apellido = ui.pedir("Apellido")
        email = ui.pedir("Email", validar=validar_email)
        password = ui.pedir("Contraseña inicial", oculto=True, validar=validar_password)
        roles = self._pedir_roles()
        id_actor = None
        if "CLIENTE" in roles:
            id_actor = ui.pedir_entero("ID del cliente al que pertenece", minimo=1)
        elif "SOCIO" in roles:                                                     # fase 9
            id_actor = ui.pedir_entero("ID del operador logístico o proveedor al que pertenece", minimo=1)
        ui.ok(f"Usuario creado con ID {self._srv.crear(nombre, apellido, email, password, roles, id_actor)}")

    def cambiar_roles(self, sesion: Sesion) -> None:
        id_usuario = ui.pedir_entero("ID del usuario", minimo=1)
        self._srv.cambiar_roles(id_usuario, self._pedir_roles())
        ui.ok("Roles actualizados. Sus sesiones abiertas se cerraron para que el cambio rija ya.")

    def bloquear(self, sesion: Sesion) -> None:
        id_usuario = ui.pedir_entero("ID del usuario", minimo=1)
        if ui.confirmar("¿Bloquear el usuario y cerrar sus sesiones?"):
            cerradas = self._srv.bloquear(id_usuario, sesion)
            ui.ok(f"Usuario bloqueado ({cerradas} sesiones cerradas)")

    def desbloquear(self, sesion: Sesion) -> None:
        self._srv.desbloquear(ui.pedir_entero("ID del usuario", minimo=1))
        ui.ok("Usuario desbloqueado")

    def cambiar_password(self, sesion: Sesion) -> None:
        id_usuario = ui.pedir_entero("ID del usuario", minimo=1)
        self._srv.cambiar_password(id_usuario, ui.pedir("Nueva contraseña", oculto=True, validar=validar_password))
        ui.ok("Contraseña cambiada; sus sesiones se cerraron")

    # ------------------------------------------------------------------ roles (fase 9)
    def alta_rol(self, sesion: Sesion) -> None:
        nombre = ui.pedir("Nombre del rol (MAYÚSCULAS, por ejemplo AUDITOR)")
        nivel = ui.pedir_entero("Nivel de permisos (1 consulta ... 5 administrador)", minimo=1, maximo=5)
        self._srv.crear_rol(nombre, nivel, ui.pedir("Descripción", obligatorio=False) or "")
        ui.ok(f"Rol {nombre.upper()} creado")

    def modificar_rol(self, sesion: Sesion) -> None:
        self.roles(sesion)
        rol = next((r for r in self._srv.roles() if r.nombre == ui.pedir("Rol a modificar").strip().upper()), None)
        if rol is None:
            ui.error("No existe ese rol")
            return
        nivel = ui.pedir_entero(f"Nivel de permisos (Enter = {rol.nivel_permisos})", minimo=1, maximo=5,
                                obligatorio=False) or rol.nivel_permisos
        descripcion = ui.pedir("Descripción", obligatorio=False, defecto=rol.descripcion or None) or ""
        cerradas = self._srv.modificar_rol(rol.nombre, nivel, descripcion)
        ui.ok(f"Rol {rol.nombre} modificado" + (f"; se cerraron {cerradas} sesiones para que rija el nuevo nivel"
                                                 if cerradas else ""))

    def baja_rol(self, sesion: Sesion) -> None:
        nombre = ui.pedir("Rol a borrar").strip().upper()
        if ui.confirmar(f"¿Borrar el rol {nombre}?"):
            self._srv.eliminar_rol(nombre)
            ui.ok(f"Rol {nombre} borrado")

    # ------------------------------------------------------------------ sesiones (fase 9)
    def sesiones(self, sesion: Sesion) -> None:
        filas, self._ultimas = [], self._srv.sesiones_activas()
        for n, s in enumerate(self._ultimas, 1):
            seg = s["segundos_restantes"]
            vence = f"{seg // 60} min" if seg >= 0 else "[yellow]vencida en Redis[/yellow]"
            yo = " (vos)" if s["id_sesion"] == sesion.id_sesion else ""
            filas.append((n, s["id_sesion"][:8], s["email"] + yo, ui.fecha(s["fecha_inicio"]), vence))
        ui.tabla(["#", "Sesión", "Usuario", "Inicio", "Vence en"], filas, f"{len(filas)} sesiones activas")

    def cerrar_sesion(self, sesion: Sesion) -> None:
        self.sesiones(sesion)
        if not self._ultimas:
            return
        n = ui.pedir_entero("Número (#) de la sesión a cerrar", minimo=1, maximo=len(self._ultimas))
        self._srv.cerrar_sesion_de_otro(self._ultimas[n - 1]["id_sesion"], sesion)
        ui.ok("Sesión cerrada: esa persona tiene que volver a iniciar sesión")

    def cerrar_sesiones_usuario(self, sesion: Sesion) -> None:
        id_usuario = ui.pedir_entero("ID del usuario", minimo=1)
        if ui.confirmar("¿Cerrar todas sus sesiones?"):
            ui.ok(f"{self._srv.cerrar_sesiones_de(id_usuario)} sesiones cerradas")
