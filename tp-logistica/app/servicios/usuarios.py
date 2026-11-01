"""Administración de usuarios y roles (solo nivel 5)."""
from __future__ import annotations

import logging
import re

from app.dominio.modelos import Sesion, Usuario
from app.dominio.validaciones import validar_email
from app.excepciones import EntidadNoEncontrada, ErrorConflicto, ErrorValidacion
from app.servicios import seguridad

log = logging.getLogger(__name__)

# Los roles que usa el propio código (permisos y filtros): se pueden modificar, no borrar
ROLES_SISTEMA = ("ADMINISTRADOR", "SUPERVISOR", "OPERADOR", "ANALISTA", "CLIENTE", "SOCIO")
_RE_ROL = re.compile(r"^[A-Z][A-Z_]{2,29}$")


def validar_password(password: str) -> str | None:
    if len(password) < 8 or not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        return "La contraseña debe tener al menos 8 caracteres, con letras y números"
    return None


class ServicioUsuarios:
    def __init__(self, repo_usuarios, repo_actores, autenticacion):
        self._repo = repo_usuarios
        self._actores = repo_actores
        self._auth = autenticacion

    def listar(self) -> list[Usuario]:
        return self._repo.listar()

    def roles(self):
        return self._repo.listar_roles()

    def _usuario(self, id_usuario: int) -> Usuario:
        u = self._repo.obtener(id_usuario)
        if u is None:
            raise EntidadNoEncontrada(f"No existe el usuario {id_usuario}")
        return u

    def _validar_roles(self, roles: list[str], id_actor: int | None) -> None:
        existentes = {r.nombre for r in self._repo.listar_roles()}
        if not roles:
            raise ErrorValidacion("Asigná al menos un rol")
        desconocidos = [r for r in roles if r not in existentes]
        if desconocidos:
            raise ErrorValidacion(f"Roles inexistentes: {', '.join(desconocidos)}")
        if "CLIENTE" in roles:
            actor = self._actores.obtener(id_actor) if id_actor else None
            if actor is None or actor.tipo != "CLIENTE":
                raise ErrorValidacion("Un usuario con rol CLIENTE debe estar asociado a un cliente existente")
        if "SOCIO" in roles:                                  # fase 9: operadores logísticos y proveedores
            actor = self._actores.obtener(id_actor) if id_actor else None
            if actor is None or actor.tipo not in ("OPERADOR", "PROVEEDOR"):
                raise ErrorValidacion("Un usuario con rol SOCIO debe estar asociado a un operador o proveedor")
        if id_actor is not None and not {"CLIENTE", "SOCIO"} & set(roles):
            raise ErrorValidacion("Un usuario de un cliente, operador o proveedor lleva el rol CLIENTE o SOCIO")

    def crear(self, nombre: str, apellido: str, email: str, password: str, roles: list[str],
              id_actor: int | None = None) -> int:
        nombre, apellido, email = nombre.strip(), apellido.strip(), email.strip().lower()
        if not nombre or not apellido:
            raise ErrorValidacion("Nombre y apellido son obligatorios")
        for problema in (validar_email(email), validar_password(password)):
            if problema:
                raise ErrorValidacion(problema)
        self._validar_roles(roles, id_actor)
        id_usuario = self._repo.crear(nombre, apellido, email, seguridad.hashear(password), roles, id_actor)
        log.info("Alta del usuario %s (%s) con roles %s", id_usuario, email, roles)
        return id_usuario

    def cambiar_roles(self, id_usuario: int, roles: list[str]) -> None:
        usuario = self._usuario(id_usuario)
        self._validar_roles(roles, usuario.id_actor)
        self._repo.reemplazar_roles(id_usuario, roles)
        # Los permisos se copian a la sesión al iniciarla: para que el cambio rija ya, se revocan
        self._auth.revocar_sesiones_de_usuario(id_usuario)
        log.info("Roles del usuario %s cambiados a %s", id_usuario, roles)

    def bloquear(self, id_usuario: int, quien: Sesion) -> int:
        if id_usuario == quien.id_usuario:
            raise ErrorConflicto("No podés bloquear tu propio usuario")
        usuario = self._usuario(id_usuario)
        if usuario.estado == "BLOQUEADO":
            raise ErrorConflicto("El usuario ya está bloqueado")
        self._repo.cambiar_estado(id_usuario, "BLOQUEADO")
        cerradas = self._auth.revocar_sesiones_de_usuario(id_usuario)   # PostgreSQL + Redis
        log.warning("Usuario %s bloqueado por %s (%d sesiones revocadas)", id_usuario, quien.email, cerradas)
        return cerradas

    def desbloquear(self, id_usuario: int) -> None:
        self._usuario(id_usuario)
        self._repo.cambiar_estado(id_usuario, "ACTIVO")
        log.info("Usuario %s desbloqueado", id_usuario)

    def cambiar_password(self, id_usuario: int, nueva: str) -> None:
        problema = validar_password(nueva)
        if problema:
            raise ErrorValidacion(problema)
        self._usuario(id_usuario)
        self._repo.cambiar_password(id_usuario, seguridad.hashear(nueva))
        self._auth.revocar_sesiones_de_usuario(id_usuario)
        log.info("Contraseña del usuario %s cambiada", id_usuario)

    # ------------------------------------------------------------------ ABM de roles (fase 9)
    def _validar_rol(self, nombre: str, nivel: int, descripcion: str) -> tuple[str, str]:
        nombre, descripcion = (nombre or "").strip().upper(), (descripcion or "").strip()
        if not _RE_ROL.match(nombre):
            raise ErrorValidacion("El nombre del rol va en mayúsculas, de 3 a 30 letras (puede llevar _)")
        if not 1 <= nivel <= 5:
            raise ErrorValidacion("El nivel de permisos va de 1 (consulta) a 5 (administrador)")
        if len(descripcion) > 200:
            raise ErrorValidacion("La descripción tiene hasta 200 caracteres")
        return nombre, descripcion

    def _rol(self, nombre: str):
        rol = next((r for r in self._repo.listar_roles() if r.nombre == nombre.strip().upper()), None)
        if rol is None:
            raise EntidadNoEncontrada(f"No existe el rol {nombre}")
        return rol

    def crear_rol(self, nombre: str, nivel: int, descripcion: str) -> int:
        nombre, descripcion = self._validar_rol(nombre, nivel, descripcion)
        id_rol = self._repo.crear_rol(nombre, nivel, descripcion)
        log.info("Alta del rol %s (nivel %s)", nombre, nivel)
        return id_rol

    def modificar_rol(self, nombre: str, nivel: int, descripcion: str) -> int:
        """Cambia nivel y descripción. Si cambia el nivel, se cierran las sesiones de quienes tienen
        ese rol (el nivel se copia a la sesión al iniciarla). Devuelve cuántas sesiones cerró."""
        rol = self._rol(nombre)
        nombre, descripcion = self._validar_rol(rol.nombre, nivel, descripcion)
        if nombre == "ADMINISTRADOR" and nivel != 5:
            raise ErrorConflicto("El rol ADMINISTRADOR tiene que seguir con nivel 5 (si no, nadie podría administrar)")
        self._repo.modificar_rol(nombre, nivel, descripcion)
        cerradas = 0
        if nivel != rol.nivel_permisos:
            for id_usuario in self._repo.usuarios_con_rol(nombre):
                cerradas += self._auth.revocar_sesiones_de_usuario(id_usuario)
        log.info("Rol %s: nivel %s -> %s (%d sesiones cerradas)", nombre, rol.nivel_permisos, nivel, cerradas)
        return cerradas

    def eliminar_rol(self, nombre: str) -> None:
        rol = self._rol(nombre)
        if rol.nombre in ROLES_SISTEMA:
            raise ErrorConflicto(f"{rol.nombre} es un rol del sistema: se puede modificar, no borrar")
        usuarios = self._repo.usuarios_con_rol(rol.nombre)
        if usuarios:
            raise ErrorConflicto(f"El rol lo tienen {len(usuarios)} usuarios (por ejemplo, el {usuarios[0]}): "
                                 "cambiales el rol antes de borrarlo")
        self._repo.eliminar_rol(rol.nombre)
        log.info("Baja del rol %s", rol.nombre)

    # ------------------------------------------------------------------ sesiones activas (fase 9)
    def sesiones_activas(self) -> list[dict]:
        return self._auth.sesiones_activas()

    def cerrar_sesion_de_otro(self, id_sesion: str, quien: Sesion) -> None:
        if id_sesion == quien.id_sesion:
            raise ErrorConflicto("Esa es tu propia sesión: cerrala con 0 en el menú principal")
        self._auth.revocar_sesion(id_sesion)

    def cerrar_sesiones_de(self, id_usuario: int) -> int:
        self._usuario(id_usuario)
        return self._auth.revocar_sesiones_de_usuario(id_usuario)
