"""Trazabilidad de envíos e incidentes (fase 4).

Tres piezas:

1. ProyeccionTrazabilidad: convierte los eventos del outbox (que ya viajan por el stream de
   Redis para Neo4j) en EVENTOS LOGÍSTICOS de MongoDB, con su propio grupo de consumidores.
   Así la línea de tiempo del envío se arma sola, sin que los servicios de la fase 2 escriban
   en MongoDB (no hay "doble escritura" que pueda quedar a medias).

2. ServicioTrazabilidad.registrar_incidente: el incidente se guarda en MongoDB con la marca
   pendiente_grafo; el sincronizador lo lleva a Neo4j, donde suma en la ruta del tramo.

3. ServicioTrazabilidad.seguimiento: una consulta POLÍGLOTA que junta, para un envío,
   PostgreSQL (datos y ruta), MongoDB (eventos e incidentes) y Cassandra (última posición GPS).
   Si MongoDB o Cassandra no responden, se muestra lo demás.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from app.dominio.modelos import (ESTADOS_INCIDENTE_ABIERTOS, TIPOS_EVENTO, TIPOS_INCIDENTE, TRANSICIONES_INCIDENTE,
                                 Envio, EventoLogistico, Incidente, Sesion)
from app.excepciones import EntidadNoEncontrada, ErrorConexion, ErrorConflicto, ErrorValidacion

log = logging.getLogger(__name__)

CLAVE_RANKING = "cache:ranking:rutas"
CACHE_SEGUNDOS = 600

# Cómo se traduce cada cambio de estado del envío a un evento logístico
EVENTO_POR_ESTADO = {"EN_TRANSITO": "EN_TRANSITO", "EN_ADUANA": "ADUANA", "DEMORADO": "RETRASO",
                     "ENTREGADO": "ENTREGADO", "CANCELADO": "OTRO"}


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def _fecha(texto: str) -> datetime:
    return datetime.fromisoformat(texto)


def ubicacion_en(envio: Envio, momento: datetime) -> str:
    """Dónde estaba el envío en `momento`: el destino del último tramo que ya había llegado
    (según la llegada real), o el origen si todavía no llegó ninguno."""
    codigo = envio.origen
    for t in envio.tramos:
        if t.fecha_llegada_real is not None and t.fecha_llegada_real <= momento:
            codigo = t.destino
    return codigo


class ProyeccionTrazabilidad:
    """Destino del sincronizador (grupo "trazabilidad"): outbox -> eventos_logisticos de MongoDB."""

    def __init__(self, repo_eventos, repo_envios, repo_catalogos):
        self._eventos = repo_eventos
        self._envios = repo_envios
        self._catalogos = repo_catalogos

    def verificar(self) -> None:
        self._eventos.verificar()

    def aplicar_evento(self, tipo_evento: str, payload: dict, id_evento: str | None = None) -> None:
        traducido = self._traducir(tipo_evento, payload)
        if traducido is None:
            return                                         # no es un hito de la línea de tiempo
        origen = int(id_evento) if id_evento and str(id_evento).isdigit() else None
        self._eventos.registrar(traducido, id_evento_origen=origen)

    def _traducir(self, tipo_evento: str, p: dict) -> EventoLogistico | None:
        if tipo_evento == "ENVIO_CREADO":
            envio = self._envios.obtener(p["id_envio"])
            if envio is None:
                return None
            return self._evento(envio, "CARGA_INICIADA", _fecha(p["fecha_creacion"]), envio.origen,
                                f"Envío creado: {envio.origen} → {envio.destino}",
                                {"contenedores": [c["id_contenedor"] for c in p.get("contenedores", [])]})
        if tipo_evento == "ENVIO_ESTADO" and p["estado"] in EVENTO_POR_ESTADO:
            envio = self._envios.obtener(p["id_envio"])
            if envio is None:
                return None
            fecha = _fecha(p["fecha"])
            lugar = envio.destino if p["estado"] == "ENTREGADO" else ubicacion_en(envio, fecha)
            return self._evento(envio, EVENTO_POR_ESTADO[p["estado"]], fecha, lugar,
                                f"El envío pasa a {p['estado']}", {"estado": p["estado"]})
        if tipo_evento == "TRAMO_LLEGADA":
            tramo = self._envios.tramo(p["id_tramo"])
            envio = self._envios.obtener(tramo.id_envio) if tramo else None
            if envio is None:
                return None
            return self._evento(envio, "DESCARGA", _fecha(p["fecha"]), tramo.destino,
                                f"Llegó el tramo {tramo.orden} ({tramo.origen} → {tramo.destino})",
                                {"id_tramo": tramo.id_tramo, "demora_h": p["demora_h"]})
        if tipo_evento == "ENVIO_MODIFICADO":              # fase 9
            envio = self._envios.obtener(p["id_envio"])
            if envio is None:
                return None
            fecha = _fecha(p["fecha"])
            return self._evento(envio, "OTRO", fecha, ubicacion_en(envio, fecha),
                                f"Cambio: prioridad {p['prioridad']}, entrega estimada {p['fecha_entrega_estimada'][:16]}",
                                {"prioridad": p["prioridad"], "fecha_entrega_estimada": p["fecha_entrega_estimada"]})
        return None                                        # ACTOR_GUARDADO, TRAMO_AGREGADO, etc.

    def _evento(self, envio: Envio, tipo: str, fecha: datetime, codigo: str, descripcion: str,
                detalle: dict) -> EventoLogistico:
        u = self._catalogos.ubicacion(codigo)
        return EventoLogistico(envio.id_envio, tipo, fecha, u.codigo, u.pais, u.latitud, u.longitud,
                               nombre_ubicacion=u.nombre, descripcion=descripcion, detalle=detalle)


class ServicioTrazabilidad:
    def __init__(self, repo_envios, repo_catalogos, repo_eventos, repo_incidentes, repo_cache=None,
                 repo_telemetria=None, repo_grafo=None, reloj: Callable[[], datetime] = _ahora,
                 carpeta_mapas: Path | None = None):
        self._envios = repo_envios
        self._catalogos = repo_catalogos
        self._eventos = repo_eventos
        self._incidentes = repo_incidentes
        self._cache = repo_cache
        self._telemetria = repo_telemetria
        self._grafo = repo_grafo
        self._reloj = reloj
        self._carpeta_mapas = carpeta_mapas or Path("mapas")

    def _envio(self, id_envio: int, id_cliente: int | None = None) -> Envio:
        envio = self._envios.obtener(id_envio)
        if envio is None or (id_cliente is not None and envio.id_cliente != id_cliente):
            raise EntidadNoEncontrada(f"No existe el envío {id_envio}")
        return envio

    # ------------------------------------------------------------------ eventos
    def registrar_evento(self, id_envio: int, tipo: str, codigo_ubicacion: str, descripcion: str,
                         sesion: Sesion, fecha: datetime | None = None) -> str:
        """Un hito cargado a mano por un operador (por ejemplo, una inspección en la aduana)."""
        envio = self._envio(id_envio)
        if tipo not in TIPOS_EVENTO:
            raise ErrorValidacion(f"Tipo inválido; opciones: {', '.join(TIPOS_EVENTO)}")
        u = self._catalogos.ubicacion(codigo_ubicacion.strip().upper())
        if u is None:
            raise EntidadNoEncontrada(f"No existe la ubicación {codigo_ubicacion}")
        fecha = fecha or self._reloj()
        if fecha < envio.fecha_creacion or fecha > self._reloj():
            raise ErrorValidacion("La fecha tiene que estar entre la creación del envío y ahora")
        if not descripcion.strip():
            raise ErrorValidacion("Describí qué pasó")
        e = EventoLogistico(id_envio, tipo, fecha, u.codigo, u.pais, u.latitud, u.longitud, u.nombre,
                            descripcion.strip(), {"manual": True}, sesion.id_usuario, sesion.nombre)
        self._eventos.registrar(e)
        log.info("Evento %s registrado a mano en el envío %s por %s", tipo, id_envio, sesion.email)
        return e.id_evento

    def eventos_cerca(self, codigo_ubicacion: str, km: float) -> list[EventoLogistico]:
        u = self._catalogos.ubicacion(codigo_ubicacion.strip().upper())
        if u is None:
            raise EntidadNoEncontrada(f"No existe la ubicación {codigo_ubicacion}")
        if not 0 < km <= 5000:
            raise ErrorValidacion("El radio tiene que estar entre 0 y 5000 km")
        return self._eventos.cerca_de(u.latitud, u.longitud, km)

    # ------------------------------------------------------------------ incidentes
    def _tramo_por_defecto(self, envio: Envio, momento: datetime) -> int | None:
        """El tramo en viaje en ese momento (salió y no llegó); si no hay, el último que salió."""
        candidato = None
        for t in envio.tramos:
            if t.fecha_salida <= momento:
                candidato = t.id_tramo
                if t.fecha_llegada_real is None or t.fecha_llegada_real > momento:
                    return t.id_tramo
        return candidato

    def registrar_incidente(self, id_envio: int, tipo: str, severidad: int, descripcion: str, sesion: Sesion,
                            id_tramo: int | None = None) -> str:
        envio = self._envio(id_envio)
        if envio.estado in ("CREADO", "CANCELADO"):
            raise ErrorConflicto(f"El envío está {envio.estado}: todavía no viaja (o no viajará)")
        if tipo not in TIPOS_INCIDENTE:
            raise ErrorValidacion(f"Tipo inválido; opciones: {', '.join(TIPOS_INCIDENTE)}")
        if not 1 <= severidad <= 5:
            raise ErrorValidacion("La severidad va de 1 (leve) a 5 (grave)")
        ahora = self._reloj()
        if id_tramo is None:
            id_tramo = self._tramo_por_defecto(envio, ahora)
        tramo = next((t for t in envio.tramos if t.id_tramo == id_tramo), None)
        if id_tramo is not None and tramo is None:
            raise ErrorValidacion(f"El tramo {id_tramo} no es de este envío")
        origen, destino = self._catalogos.ubicacion(envio.origen), self._catalogos.ubicacion(envio.destino)
        incidente = Incidente(id_envio, tipo, severidad, ahora, envio.id_cliente, origen.pais, destino.pais,
                              f"{envio.origen}-{envio.destino}", descripcion.strip(), id_tramo,
                              tramo.medio_transporte if tramo else None)
        self._incidentes.insertar(incidente, sesion.id_usuario)
        self._invalidar_ranking()
        log.warning("Incidente %s (severidad %s) en el envío %s", tipo, severidad, id_envio)
        return incidente.id_incidente

    def cambiar_estado_incidente(self, id_incidente: str, nuevo: str, sesion: Sesion) -> None:
        try:
            incidente = self._incidentes.obtener(id_incidente)
        except ErrorConexion:
            raise
        except Exception as exc:  # noqa: BLE001   (un ID con formato inválido)
            raise ErrorValidacion("El ID del incidente no es válido") from exc
        if incidente is None:
            raise EntidadNoEncontrada(f"No existe el incidente {id_incidente}")
        permitidos = TRANSICIONES_INCIDENTE.get(incidente.estado, ())
        if nuevo not in permitidos:
            raise ErrorConflicto(f"Un incidente {incidente.estado} no puede pasar a {nuevo}. "
                                 f"Opciones: {', '.join(permitidos) or 'ninguna (estado final)'}")
        self._incidentes.cambiar_estado(id_incidente, nuevo, self._reloj(), sesion.id_usuario)
        self._invalidar_ranking()

    def incidentes(self, estados: list[str] | None = None) -> list[Incidente]:
        return self._incidentes.listar(estados or list(ESTADOS_INCIDENTE_ABIERTOS))

    def incidentes_de_envio(self, id_envio: int, id_cliente: int | None = None) -> list[Incidente]:
        self._envio(id_envio, id_cliente)
        return self._incidentes.de_envio(id_envio)

    # ------------------------------------------------------------------ rankings
    def _invalidar_ranking(self) -> None:
        if self._cache is None:
            return
        try:
            self._cache.borrar(CLAVE_RANKING)
        except Exception as exc:  # noqa: BLE001
            log.warning("No se pudo invalidar el ranking en Redis (vence solo en 10 min): %s", exc)

    def ranking_rutas(self) -> tuple[list[dict], bool]:
        """(filas, vino_de_cache). Cache-aside: Redis primero; si no está, MongoDB y se guarda 10 min."""
        if self._cache is not None:
            try:
                guardado = self._cache.obtener(CLAVE_RANKING)
                if guardado is not None:
                    return guardado, True
            except Exception as exc:  # noqa: BLE001   (sin Redis se calcula igual)
                log.warning("Caché no disponible: %s", exc)
        filas = self._incidentes.ranking_rutas()
        if self._cache is not None:
            try:
                self._cache.guardar(CLAVE_RANKING, filas, CACHE_SEGUNDOS)
            except Exception:  # noqa: BLE001
                pass
        return filas, False

    def rutas_problematicas(self) -> list[dict]:
        return self._grafo.rutas_problematicas() if self._grafo else []

    def ruta_alternativa(self, desde: str, hasta: str, tasa_maxima: float) -> dict | None:
        if self._grafo is None:
            return None
        return self._grafo.ruta_alternativa(desde.strip().upper(), hasta.strip().upper(), tasa_maxima)

    # ------------------------------------------------------------------ seguimiento
    def seguimiento(self, id_envio: int, id_cliente: int | None = None) -> dict:
        """Todo lo que sabemos de un envío, de cuatro motores. Lo que no responde se informa aparte."""
        envio = self._envio(id_envio, id_cliente)                     # PostgreSQL: obligatorio
        resultado = {"envio": envio, "eventos": [], "incidentes": [], "posicion": None, "no_disponible": []}
        try:
            resultado["eventos"] = self._eventos.de_envio(id_envio)
            resultado["incidentes"] = self._incidentes.de_envio(id_envio)
        except Exception as exc:  # noqa: BLE001
            log.warning("Seguimiento sin MongoDB: %s", exc)
            resultado["no_disponible"].append("MongoDB (eventos e incidentes)")
        if self._telemetria is not None:
            try:
                resultado["posicion"] = self._telemetria.ultima_posicion_envio(id_envio)
            except Exception as exc:  # noqa: BLE001
                log.warning("Seguimiento sin Cassandra: %s", exc)
                resultado["no_disponible"].append("Cassandra (última posición)")
        return resultado

    # ------------------------------------------------------------------ fase 9: contenedores, mapas y red
    def historial_contenedor(self, id_contenedor: int) -> list[dict]:
        """Todos los envíos en que viajó el contenedor, con su recorrido (Neo4j: VIAJA_EN desde/hasta)."""
        if self._grafo is None:
            return []
        return self._grafo.historial_contenedor(id_contenedor)

    def red_cliente(self, id_cliente: int) -> list[dict]:
        if self._grafo is None:
            return []
        return self._grafo.red_cliente(id_cliente)

    def _guardar_mapa(self, nombre: str, titulo: str, puntos: list[dict]) -> Path:
        from app.servicios.mapas import html_mapa
        if not puntos:
            raise EntidadNoEncontrada("No hay posiciones GPS registradas (Cassandra) para mostrar en el mapa")
        self._carpeta_mapas.mkdir(parents=True, exist_ok=True)
        ruta = self._carpeta_mapas / nombre
        ruta.write_text(html_mapa(titulo, puntos), encoding="utf-8")
        return ruta

    def mapa_envio(self, id_envio: int, id_cliente: int | None = None) -> Path:
        """Mapa HTML del recorrido GPS de un envío (las posiciones salen de Cassandra)."""
        envio = self._envio(id_envio, id_cliente)
        puntos = self._telemetria.recorrido_envio(id_envio) if self._telemetria else []
        return self._guardar_mapa(f"envio_{id_envio}.html", f"Envío {id_envio}: {envio.origen} → {envio.destino}",
                                  puntos)

    def mapa_contenedor(self, id_contenedor: int) -> Path:
        """Mapa con todos los viajes de un contenedor: los envíos salen de Neo4j y las posiciones de Cassandra."""
        puntos = []
        for viaje in self.historial_contenedor(id_contenedor):
            for p in self._telemetria.recorrido_envio(viaje["id_envio"]) if self._telemetria else []:
                if p["id_contenedor"] == id_contenedor:
                    puntos.append({**p, "envio": viaje["id_envio"]})
        return self._guardar_mapa(f"contenedor_{id_contenedor}.html", f"Recorrido del contenedor {id_contenedor}",
                                  puntos)
