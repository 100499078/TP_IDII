"""Usuarios y roles en PostgreSQL."""
from __future__ import annotations

import psycopg

from app.dominio.modelos import Rol, Usuario
from app.excepciones import ErrorConflicto
from app.infra.conexiones import GestorConexiones


class RepositorioUsuarios:
    def __init__(self, conexiones: GestorConexiones):
        self._cx = conexiones

    def buscar_por_email(self, email: str) -> tuple[Usuario, str] | None:
        """Devuelve el usuario con sus roles y el hash de su contraseña, o None."""
        with self._cx.transaccion() as cur:
            cur.execute(
                """SELECT id_usuario, id_actor, nombre, apellido, email, password_hash,
                          estado, fecha_registro
                     FROM usuario WHERE lower(email) = lower(%s)""",
                (email,))
            fila = cur.fetchone()
            if fila is None:
                return None
            roles = self._roles(cur, fila["id_usuario"])
        usuario = Usuario(id_usuario=fila["id_usuario"], nombre=fila["nombre"], apellido=fila["apellido"],
                          email=fila["email"], estado=fila["estado"], id_actor=fila["id_actor"],
                          roles=roles, fecha_registro=fila["fecha_registro"])
        return usuario, fila["password_hash"]

    def crear(self, nombre: str, apellido: str, email: str, password_hash: str,
              roles: list[str], id_actor: int | None = None) -> int:
        """Crea el usuario y le asigna roles en UNA transacción: o queda todo o nada."""
        try:
            with self._cx.transaccion() as cur:
                cur.execute(
                    """INSERT INTO usuario (id_actor, nombre, apellido, email, password_hash)
                       VALUES (%s, %s, %s, %s, %s) RETURNING id_usuario""",
                    (id_actor, nombre, apellido, email, password_hash))
                id_usuario = cur.fetchone()["id_usuario"]
                for rol in roles:
                    cur.execute(
                        """INSERT INTO usuario_rol (id_usuario, id_rol)
                           SELECT %s, id_rol FROM rol WHERE nombre = %s""", (id_usuario, rol))
                    if cur.rowcount == 0:
                        raise ErrorConflicto(f"El rol {rol} no existe")
                return id_usuario
        except psycopg.errors.UniqueViolation as exc:
            raise ErrorConflicto(f"Ya existe un usuario con el email {email}") from exc

    def asegurar_rol(self, nombre: str, nivel: int, descripcion: str) -> None:
        """Crea el rol si no existe (idempotente: se puede ejecutar muchas veces)."""
        with self._cx.transaccion() as cur:
            cur.execute(
                """INSERT INTO rol (nombre, nivel_permisos, descripcion) VALUES (%s, %s, %s)
                   ON CONFLICT (nombre) DO NOTHING""", (nombre, nivel, descripcion))

    # ------------------------------------------------------------------ administración (fase 2)
    def listar(self) -> list[Usuario]:
        with self._cx.transaccion() as cur:
            cur.execute("""SELECT id_usuario, id_actor, nombre, apellido, email, estado, fecha_registro
                             FROM usuario ORDER BY apellido, nombre""")
            filas = cur.fetchall()
            return [Usuario(id_usuario=f["id_usuario"], nombre=f["nombre"], apellido=f["apellido"],
                            email=f["email"], estado=f["estado"], id_actor=f["id_actor"],
                            roles=self._roles(cur, f["id_usuario"]), fecha_registro=f["fecha_registro"])
                    for f in filas]

    def obtener(self, id_usuario: int) -> Usuario | None:
        return next((u for u in self.listar() if u.id_usuario == id_usuario), None)

    def listar_roles(self) -> list[Rol]:
        with self._cx.transaccion() as cur:
            cur.execute("SELECT id_rol, nombre, nivel_permisos, descripcion FROM rol ORDER BY nivel_permisos DESC")
            return [Rol(**f) for f in cur.fetchall()]

    # ------------------------------------------------------------------ ABM de roles (fase 9)
    def crear_rol(self, nombre: str, nivel: int, descripcion: str) -> int:
        import psycopg
        try:
            with self._cx.transaccion() as cur:
                cur.execute("INSERT INTO rol (nombre, nivel_permisos, descripcion) VALUES (%s, %s, %s) "
                            "RETURNING id_rol", (nombre, nivel, descripcion))
                return cur.fetchone()["id_rol"]
        except psycopg.errors.UniqueViolation as exc:
            raise ErrorConflicto(f"Ya existe el rol {nombre}") from exc

    def modificar_rol(self, nombre: str, nivel: int, descripcion: str) -> bool:
        with self._cx.transaccion() as cur:
            cur.execute("UPDATE rol SET nivel_permisos = %s, descripcion = %s WHERE nombre = %s",
                        (nivel, descripcion, nombre))
            return cur.rowcount == 1

    def usuarios_con_rol(self, nombre: str) -> list[int]:
        with self._cx.transaccion() as cur:
            cur.execute("""SELECT ur.id_usuario FROM usuario_rol ur JOIN rol r USING (id_rol)
                            WHERE r.nombre = %s ORDER BY 1""", (nombre,))
            return [f["id_usuario"] for f in cur.fetchall()]

    def eliminar_rol(self, nombre: str) -> bool:
        """Borra el rol. La FK de usuario_rol impide borrar un rol que alguien tiene asignado."""
        with self._cx.transaccion() as cur:
            cur.execute("DELETE FROM rol WHERE nombre = %s", (nombre,))
            return cur.rowcount == 1

    def cambiar_estado(self, id_usuario: int, estado: str) -> None:
        with self._cx.transaccion() as cur:
            cur.execute("UPDATE usuario SET estado = %s WHERE id_usuario = %s", (estado, id_usuario))

    def reemplazar_roles(self, id_usuario: int, roles: list[str]) -> None:
        """Borra los roles actuales y asigna los nuevos en una transacción."""
        with self._cx.transaccion() as cur:
            cur.execute("DELETE FROM usuario_rol WHERE id_usuario = %s", (id_usuario,))
            for rol in roles:
                cur.execute("""INSERT INTO usuario_rol (id_usuario, id_rol)
                               SELECT %s, id_rol FROM rol WHERE nombre = %s""", (id_usuario, rol))
                if cur.rowcount == 0:
                    raise ErrorConflicto(f"El rol {rol} no existe")

    def cambiar_password(self, id_usuario: int, password_hash: str) -> None:
        with self._cx.transaccion() as cur:
            cur.execute("UPDATE usuario SET password_hash = %s WHERE id_usuario = %s", (password_hash, id_usuario))

    @staticmethod
    def _roles(cur, id_usuario: int) -> list[Rol]:
        cur.execute(
            """SELECT r.id_rol, r.nombre, r.nivel_permisos, r.descripcion
                 FROM rol r JOIN usuario_rol ur USING (id_rol)
                WHERE ur.id_usuario = %s ORDER BY r.nivel_permisos DESC""", (id_usuario,))
        return [Rol(**f) for f in cur.fetchall()]
