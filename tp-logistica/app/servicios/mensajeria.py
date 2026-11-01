"""Mensajería entre usuarios: conversaciones privadas y grupos de trabajo (fase 5).

Reglas:
- Un usuario de un cliente (rol CLIENTE) solo puede escribirle a usuarios internos de la
  empresa, y no crea grupos. Así un cliente no ve ni contacta a otros clientes.
- Los grupos de trabajo son internos: solo integran usuarios de la empresa.
- Solo se lee y se escribe en conversaciones de las que uno participa.
- Grupos: el que lo crea es ADMIN; solo un ADMIN del grupo (o un administrador del sistema)
  agrega o quita integrantes. Un grupo tiene al menos 2 integrantes.
- Como mucho 20 mensajes por minuto por usuario (limitador en Redis).

Motores: MongoDB guarda todo (fuente de verdad); Redis lleva los contadores de no leídos, el
límite de frecuencia y la presencia; PostgreSQL dice quiénes son los usuarios.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable

from app.dominio.modelos import Conversacion, GrupoTrabajo, Integrante, Mensaje, Sesion
from app.excepciones import EntidadNoEncontrada, ErrorConexion, ErrorConflicto, ErrorPermiso, ErrorValidacion

log = logging.getLogger(__name__)

LARGO_MAXIMO = 4000


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


class ServicioMensajeria:
    def __init__(self, repo_usuarios, repo_grupos, repo_conversaciones, repo_mensajes, repo_redis,
                 reloj: Callable[[], datetime] = _ahora):
        self._usuarios = repo_usuarios
        self._grupos = repo_grupos
        self._conversaciones = repo_conversaciones
        self._mensajes = repo_mensajes
        self._redis = repo_redis
        self._reloj = reloj

    # ------------------------------------------------------------------ contactos
    def contactos(self, sesion: Sesion, solo_internos: bool = False) -> list[dict]:
        """Usuarios activos a los que puede escribir, con su estado de conexión (Redis)."""
        candidatos = [u for u in self._usuarios.listar()
                      if u.estado == "ACTIVO" and u.id_usuario != sesion.id_usuario
                      and (sesion.id_actor is None or u.id_actor is None)     # un cliente: solo internos
                      and (not solo_internos or u.id_actor is None)]
        try:
            conectados = self._redis.en_linea([u.id_usuario for u in candidatos])
        except Exception as exc:  # noqa: BLE001   (sin Redis no se sabe, pero se puede escribir igual)
            log.warning("No se pudo consultar la presencia en Redis: %s", exc)
            conectados = set()
        return [{"usuario": u, "en_linea": u.id_usuario in conectados} for u in candidatos]

    def _usuario_destino(self, sesion: Sesion, id_destino: int, solo_internos: bool = False):
        destino = self._usuarios.obtener(id_destino)
        if destino is None or destino.estado != "ACTIVO" or destino.id_usuario == sesion.id_usuario:
            raise EntidadNoEncontrada(f"No hay un usuario activo {id_destino} para escribirle")
        if sesion.id_actor is not None and destino.id_actor is not None:
            raise ErrorPermiso("Desde una cuenta de cliente solo se puede escribir a personal de la empresa")
        if solo_internos and destino.id_actor is not None:
            raise ErrorPermiso(f"{destino.nombre_completo} es usuario de un cliente: los grupos son internos")
        return destino

    # ------------------------------------------------------------------ conversaciones
    def _conversacion(self, id_conversacion: str, sesion: Sesion) -> Conversacion:
        try:
            c = self._conversaciones.obtener(id_conversacion)
        except ErrorConexion:
            raise
        except Exception as exc:  # noqa: BLE001   (un ID con formato inválido)
            raise ErrorValidacion("El ID de la conversación no es válido") from exc
        if c is None or sesion.id_usuario not in c.participantes:
            raise EntidadNoEncontrada("No existe la conversación (o no participás de ella)")
        return c

    def bandeja(self, sesion: Sesion) -> list[dict]:
        """Conversaciones del usuario con título y cantidad de no leídos."""
        conversaciones = self._conversaciones.bandeja(sesion.id_usuario)
        no_leidos = self.no_leidos(sesion, conversaciones)
        nombres = {u.id_usuario: u.nombre_completo for u in self._usuarios.listar()}
        filas = []
        for c in conversaciones:
            if c.tipo == "GRUPAL":
                g = self._grupos.obtener(c.id_grupo)
                c.titulo = f"Grupo {g.nombre}" if g else "Grupo"
            else:
                otro = next((p for p in c.participantes if p != sesion.id_usuario), None)
                c.titulo = nombres.get(otro, f"Usuario {otro}")
            filas.append({"conversacion": c, "no_leidos": no_leidos.get(c.id_conversacion, 0)})
        return filas

    def no_leidos(self, sesion: Sesion, conversaciones: list[Conversacion] | None = None) -> dict[str, int]:
        """Contadores de Redis; si no están (Redis se reinició, se borró la clave), se recalculan
        desde MongoDB y se vuelven a guardar. Si Redis no responde, se cuentan en MongoDB."""
        try:
            conteos = self._redis.no_leidos(sesion.id_usuario)
            if conteos is not None:
                return conteos
        except Exception as exc:  # noqa: BLE001
            log.warning("Contadores de Redis no disponibles: %s", exc)
            conteos = None
        if conversaciones is None:
            conversaciones = self._conversaciones.bandeja(sesion.id_usuario, limite=200)
        conteos = self._mensajes.no_leidos_por_conversacion([c.id_conversacion for c in conversaciones],
                                                            sesion.id_usuario)
        try:
            self._redis.reemplazar_no_leidos(sesion.id_usuario, conteos)
        except Exception:  # noqa: BLE001
            pass
        return conteos

    def total_no_leidos(self, sesion: Sesion) -> int:
        return sum(self.no_leidos(sesion).values())

    def abrir(self, id_conversacion: str, sesion: Sesion, limite: int = 20) -> list[Mensaje]:
        """Últimos mensajes de la conversación; los marca como leídos (MongoDB) y limpia el contador (Redis)."""
        self._conversacion(id_conversacion, sesion)
        mensajes = self._mensajes.ultimos(id_conversacion, limite)
        self._mensajes.marcar_leidos(id_conversacion, sesion.id_usuario)
        try:
            self._redis.limpiar(sesion.id_usuario, id_conversacion)
        except Exception as exc:  # noqa: BLE001
            log.warning("No se pudo limpiar el contador en Redis: %s", exc)
        return mensajes

    def conversacion_privada(self, sesion: Sesion, id_destino: int) -> Conversacion:
        self._usuario_destino(sesion, id_destino)
        return self._conversaciones.privada(sesion.id_usuario, id_destino, self._reloj())

    # ------------------------------------------------------------------ enviar
    def enviar(self, id_conversacion: str, contenido: str, sesion: Sesion, tipo: str = "TEXTO") -> Mensaje:
        c = self._conversacion(id_conversacion, sesion)
        contenido = (contenido or "").strip()
        if not contenido:
            raise ErrorValidacion("El mensaje está vacío")
        if len(contenido) > LARGO_MAXIMO:
            raise ErrorValidacion(f"El mensaje supera los {LARGO_MAXIMO} caracteres")
        if tipo != "SISTEMA":
            try:
                if not self._redis.permitir_envio(sesion.id_usuario):
                    raise ErrorConflicto("Mandaste demasiados mensajes en el último minuto; esperá un poco")
            except ErrorConflicto:
                raise
            except Exception as exc:  # noqa: BLE001   (sin Redis no hay límite, pero se puede escribir)
                log.warning("Limitador no disponible: %s", exc)
        ahora = self._reloj()
        m = Mensaje(id_conversacion, sesion.id_usuario, sesion.nombre, ahora, contenido, tipo,
                    leido_por=[sesion.id_usuario])
        if c.tipo == "GRUPAL":
            destinatario = {"tipo": "GRUPO", "id_grupo": c.id_grupo}
        else:
            destinatario = {"tipo": "USUARIO",
                            "id_usuario": next(p for p in c.participantes if p != sesion.id_usuario)}
        self._mensajes.insertar(m, destinatario)                                   # 1) MongoDB: el mensaje
        self._conversaciones.actualizar_ultimo(id_conversacion, contenido, ahora, sesion.id_usuario)  # 2) cabecera
        otros = [p for p in c.participantes if p != sesion.id_usuario]
        try:
            self._redis.sumar_no_leidos(otros, id_conversacion)                    # 3) Redis: contadores
        except Exception as exc:  # noqa: BLE001
            log.warning("No se pudieron actualizar los no leídos en Redis (se recalculan): %s", exc)
        return m

    def enviar_privado(self, sesion: Sesion, id_destino: int, contenido: str) -> Mensaje:
        c = self.conversacion_privada(sesion, id_destino)
        return self.enviar(c.id_conversacion, contenido, sesion)

    # ------------------------------------------------------------------ grupos
    def grupos(self, sesion: Sesion) -> list[GrupoTrabajo]:
        return self._grupos.de_usuario(sesion.id_usuario)

    def crear_grupo(self, sesion: Sesion, nombre: str, descripcion: str, ids_integrantes: list[int]) -> str:
        if sesion.id_actor is not None:
            raise ErrorPermiso("Las cuentas de clientes no crean grupos de trabajo")
        nombre = (nombre or "").strip()
        if not 3 <= len(nombre) <= 80:
            raise ErrorValidacion("El nombre del grupo tiene entre 3 y 80 caracteres")
        ids = sorted(set(ids_integrantes) - {sesion.id_usuario})
        if not ids:
            raise ErrorValidacion("Un grupo necesita al menos otro integrante además de vos")
        ahora = self._reloj()
        integrantes = [Integrante(sesion.id_usuario, sesion.nombre, "ADMIN", ahora)]
        for i in ids:
            u = self._usuario_destino(sesion, i, solo_internos=True)
            integrantes.append(Integrante(u.id_usuario, u.nombre_completo, "MIEMBRO", ahora))
        grupo = GrupoTrabajo(nombre, integrantes, sesion.id_usuario, ahora, descripcion.strip())
        id_grupo = self._grupos.crear(grupo)
        id_conv = self._conversaciones.crear_grupal(id_grupo, [i.id_usuario for i in integrantes], ahora)
        self.enviar(id_conv, f"{sesion.nombre} creó el grupo {nombre}", sesion, tipo="SISTEMA")
        log.info("Grupo %s creado por %s con %d integrantes", nombre, sesion.email, len(integrantes))
        return id_grupo

    def _grupo_administrable(self, id_grupo: str, sesion: Sesion) -> tuple[GrupoTrabajo, Conversacion]:
        g = self._grupos.obtener(id_grupo)
        if g is None or not g.es_integrante(sesion.id_usuario):
            raise EntidadNoEncontrada("No existe el grupo (o no sos integrante)")
        if not g.es_admin(sesion.id_usuario) and sesion.nivel_permisos < 5:
            raise ErrorPermiso("Solo un ADMIN del grupo puede cambiar sus integrantes")
        return g, self._conversaciones.de_grupo(id_grupo)

    def agregar_integrante(self, id_grupo: str, id_usuario: int, sesion: Sesion) -> None:
        g, conv = self._grupo_administrable(id_grupo, sesion)
        u = self._usuario_destino(sesion, id_usuario, solo_internos=True)
        if not self._grupos.agregar_integrante(id_grupo, Integrante(u.id_usuario, u.nombre_completo, "MIEMBRO",
                                                                    self._reloj())):
            raise ErrorConflicto(f"{u.nombre_completo} ya está en el grupo")
        self._conversaciones.agregar_participante(conv.id_conversacion, u.id_usuario)
        self.enviar(conv.id_conversacion, f"{sesion.nombre} agregó a {u.nombre_completo}", sesion, tipo="SISTEMA")

    def quitar_integrante(self, id_grupo: str, id_usuario: int, sesion: Sesion) -> None:
        g, conv = self._grupo_administrable(id_grupo, sesion)
        if not g.es_integrante(id_usuario):
            raise EntidadNoEncontrada("Ese usuario no está en el grupo")
        if len(g.integrantes) <= 2:
            raise ErrorConflicto("Un grupo necesita al menos 2 integrantes")
        quitado = next(i for i in g.integrantes if i.id_usuario == id_usuario)
        self._grupos.quitar_integrante(id_grupo, id_usuario)
        self._conversaciones.quitar_participante(conv.id_conversacion, id_usuario)
        try:
            self._redis.limpiar(id_usuario, conv.id_conversacion)
        except Exception:  # noqa: BLE001
            pass
        self.enviar(conv.id_conversacion, f"{sesion.nombre} quitó a {quitado.nombre}", sesion, tipo="SISTEMA")
