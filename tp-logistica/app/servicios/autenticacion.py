"""Inicio y cierre de sesión: PostgreSQL y Redis trabajando juntos.

- PostgreSQL: valida usuario/contraseña y guarda la auditoría de cada sesión.
- Redis: guarda la sesión viva con TTL; se renueva en cada operación.

No hay transacciones entre motores, así que el servicio define el orden de
las escrituras y una acción de compensación si la segunda falla.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Callable

from app.dominio.modelos import Sesion
from app.excepciones import EntidadNoEncontrada, ErrorAplicacion, ErrorAutenticacion, ErrorPermiso, ErrorValidacion
from app.servicios import seguridad

log = logging.getLogger(__name__)

MENSAJE_CREDENCIALES = "Usuario o contraseña incorrectos"


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


class ServicioAutenticacion:
    def __init__(self, repo_usuarios, repo_historial, repo_sesiones, ttl_segundos: int,
                 reloj: Callable[[], datetime] = _ahora):
        self._usuarios = repo_usuarios
        self._historial = repo_historial
        self._sesiones = repo_sesiones
        self._ttl = ttl_segundos
        self._reloj = reloj

    # ------------------------------------------------------------------ inicio
    def iniciar_sesion(self, email: str, password: str) -> Sesion:
        email = (email or "").strip()
        if not email or not password:
            raise ErrorValidacion("Ingresá el email y la contraseña")

        encontrado = self._usuarios.buscar_por_email(email)
        # Mismo mensaje si el usuario no existe o la clave es incorrecta:
        # así no se revela qué emails están registrados.
        if encontrado is None or not seguridad.verificar(password, encontrado[1]):
            log.warning("Inicio de sesión fallido para %s", email)
            raise ErrorAutenticacion(MENSAJE_CREDENCIALES)
        usuario, _ = encontrado
        if usuario.estado != "ACTIVO":
            log.warning("Inicio de sesión rechazado: %s está %s", email, usuario.estado)
            raise ErrorAutenticacion(f"El usuario está {usuario.estado.lower()}")

        ahora = self._reloj()
        sesion = Sesion(id_sesion=str(uuid.uuid4()), id_usuario=usuario.id_usuario, email=usuario.email,
                        nombre=usuario.nombre_completo, roles=[r.nombre for r in usuario.roles],
                        nivel_permisos=usuario.nivel_permisos, fecha_inicio=ahora, ultimo_acceso=ahora,
                        id_actor=usuario.id_actor)

        # 1) Primero la fuente de verdad de la auditoría (PostgreSQL).
        self._historial.registrar_inicio(sesion.id_sesion, sesion.id_usuario)
        # 2) Después la sesión viva (Redis). Si falla, se compensa el paso 1.
        try:
            self._sesiones.guardar(sesion, self._ttl)
        except Exception as exc:
            log.error("No se pudo guardar la sesión en Redis; se revoca en PostgreSQL: %s", exc)
            self._historial.finalizar(sesion.id_sesion, "REVOCADA")
            if isinstance(exc, ErrorAplicacion):
                raise
            raise ErrorAplicacion("No se pudo iniciar la sesión (Redis no responde)") from exc

        log.info("Sesión %s iniciada por %s", sesion.id_sesion, email)
        return sesion

    # ------------------------------------------------------------------ validación
    def validar(self, id_sesion: str) -> Sesion:
        """Se llama antes de cada operación. Si la sesión venció en Redis, se
        registra como EXPIRADA en PostgreSQL; si sigue viva, se renueva su TTL."""
        sesion = self._sesiones.obtener(id_sesion)
        if sesion is None:
            if self._historial.finalizar(id_sesion, "EXPIRADA"):
                log.info("Sesión %s expirada por inactividad", id_sesion)
            raise ErrorAutenticacion("Tu sesión venció por inactividad. Volvé a ingresar.")
        ahora = self._reloj()
        self._sesiones.renovar(id_sesion, self._ttl, ahora)
        sesion.ultimo_acceso = ahora
        return sesion

    @staticmethod
    def exigir_nivel(sesion: Sesion, nivel_minimo: int) -> None:
        if sesion.nivel_permisos < nivel_minimo:
            raise ErrorPermiso(f"Esta opción requiere nivel de permisos {nivel_minimo} "
                               f"(tu nivel es {sesion.nivel_permisos})")

    # ------------------------------------------------------------------ cierre
    def cerrar_sesion(self, sesion: Sesion) -> None:
        # Primero Redis: la sesión deja de ser válida en el acto (seguridad).
        self._sesiones.eliminar(sesion.id_sesion, sesion.id_usuario)
        # Después la auditoría. Si PostgreSQL fallara, la sesión queda ACTIVA en el
        # historial y la corrige el proceso de reconciliación (sesiones huérfanas).
        try:
            self._historial.finalizar(sesion.id_sesion, "CERRADA")
        except Exception:  # noqa: BLE001
            log.exception("No se pudo registrar el cierre de la sesión %s", sesion.id_sesion)
        log.info("Sesión %s cerrada por %s", sesion.id_sesion, sesion.email)

    def revocar_sesiones_de_usuario(self, id_usuario: int) -> int:
        """Cierra todas las sesiones de un usuario (se usa al bloquearlo). Devuelve cuántas cerró."""
        cerradas = 0
        for id_sesion in self._sesiones.de_usuario(id_usuario):
            self._sesiones.eliminar(id_sesion, id_usuario)
            self._historial.finalizar(id_sesion, "REVOCADA")
            cerradas += 1
        if cerradas:
            log.info("Se revocaron %d sesiones del usuario %s", cerradas, id_usuario)
        return cerradas

    def sesiones_activas(self) -> list[dict]:
        """Fase 9: las sesiones ACTIVA del historial (PostgreSQL) con lo que les queda en Redis.
        Si en Redis ya no existe (segundos < 0), es una sesión vencida que todavía no se reconcilió."""
        filas = self._historial.activas()
        for f in filas:
            f["segundos_restantes"] = self._sesiones.segundos_restantes(f["id_sesion"])
        return filas

    def revocar_sesion(self, id_sesion: str) -> None:
        """Fase 9: un administrador cierra una sesión ajena (Redis primero: deja de valer en el acto)."""
        fila = next((f for f in self._historial.activas() if f["id_sesion"] == id_sesion), None)
        if fila is None:
            raise EntidadNoEncontrada("No hay una sesión activa con ese ID")
        self._sesiones.eliminar(id_sesion, fila["id_usuario"])
        self._historial.finalizar(id_sesion, "REVOCADA")
        log.warning("Sesión %s del usuario %s revocada por un administrador", id_sesion, fila["id_usuario"])

    def segundos_restantes(self, sesion: Sesion) -> int:
        return self._sesiones.segundos_restantes(sesion.id_sesion)

    # ------------------------------------------------------------------ reconciliación
    def reconciliar_sesiones(self) -> int:
        """Cierra en PostgreSQL las sesiones ACTIVA cuya clave ya no existe en Redis
        (por ejemplo, si el aviso de vencimiento se perdió). Devuelve cuántas corrigió."""
        corregidas = 0
        for fila in self._historial.activas():
            if not self._sesiones.existe(fila["id_sesion"]):
                if self._historial.finalizar(fila["id_sesion"], "EXPIRADA"):
                    corregidas += 1
        if corregidas:
            log.info("Reconciliación de sesiones: %d marcadas como EXPIRADA", corregidas)
        return corregidas
