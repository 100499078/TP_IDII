"""Sincronización PostgreSQL → Redis → Neo4j (y MongoDB desde la fase 4).

    PostgreSQL (outbox)  --publicar-->  Redis (sync:stream)  --procesar-->  Neo4j (MERGE)

1. publicar_pendientes(): lee los eventos sin publicar del outbox, los agrega al
   stream y los marca como publicados.
2. procesar_cola(): lee el stream con el grupo de consumidores, aplica cada evento
   en Neo4j y lo confirma (XACK). Si Neo4j falla, el evento queda pendiente y se
   reintenta en el ciclo siguiente, en el mismo orden.

Si Neo4j está CAÍDO (falla transitoria), no se cuenta como intento fallido: el
ciclo se corta y se reintenta más tarde. Solo un evento que falla con Neo4j
funcionando (por ejemplo, datos inválidos) suma intentos y, tras MAX_ENTREGAS,
va a la cola de fallidos; así una caída larga de Neo4j no descarta eventos.

Desde la fase 4 el mismo stream alimenta DOS proyecciones, cada una con su grupo:
Neo4j (grafo) y MongoDB (eventos logísticos de la trazabilidad).

La entrega es "al menos una vez": un evento puede aplicarse dos veces (por
ejemplo, si el proceso se corta entre XADD y marcar el outbox). No es un
problema porque cada sentencia del grafo es idempotente (MERGE).
"""
from __future__ import annotations

import json
import logging

from app.excepciones import ErrorConexion

log = logging.getLogger(__name__)

MAX_ENTREGAS = 5          # después de 5 intentos el evento va a la cola de fallidos


def es_transitorio(exc: Exception) -> bool:
    """¿El error es porque el motor no responde (y conviene esperar), o por el evento en sí?"""
    return isinstance(exc, (ErrorConexion, ConnectionError, TimeoutError))


class Proyeccion:
    """Un destino que se alimenta del stream con SU PROPIO grupo de consumidores.

    `destino` tiene que tener verificar() (falla con ErrorConexion si su motor no responde)
    y aplicar_evento(tipo, payload) (idempotente). Cada grupo recibe todos los eventos.
    """

    def __init__(self, nombre: str, cola, destino, consumidor: str = "worker-1"):
        self.nombre = nombre
        self.cola = cola
        self.destino = destino
        self.consumidor = consumidor
        self.grupo_listo = False


class ServicioSincronizacion:
    def __init__(self, repo_outbox, cola, grafo, repo_actores=None, repo_catalogos=None,
                 repo_contenedores=None, repo_envios=None, consumidor: str = "worker-1",
                 otras: list[Proyeccion] | None = None, repo_incidentes=None):
        self._outbox = repo_outbox
        self._cola = cola                    # para publicar (el stream es uno solo)
        self._grafo = grafo
        self._actores = repo_actores
        self._catalogos = repo_catalogos
        self._contenedores = repo_contenedores
        self._envios = repo_envios
        self._incidentes = repo_incidentes   # fase 4: "outbox" de incidentes dentro de MongoDB
        self.proyecciones = [Proyeccion("Neo4j", cola, grafo, consumidor)] + list(otras or [])

    # ------------------------------------------------------------------ ciclo
    def publicar_pendientes(self, limite: int = 200) -> int:
        eventos = self._outbox.pendientes(limite)
        if eventos:
            self._cola.publicar(eventos)                                       # 1) Redis
            self._outbox.marcar_publicados([e["id_evento"] for e in eventos])  # 2) PostgreSQL
            log.info("Publicados %d eventos del outbox en el stream", len(eventos))
        # Fase 8: los incidentes se publican recién cuando el outbox quedó vacío. Un incidente se
        # refiere a un tramo, y el TRAMO_AGREGADO tiene que entrar antes al stream: con mucho atraso
        # acumulado (Neo4j caído un rato, o la carga de datos de prueba) el incidente llegaba primero
        # y en Neo4j no encontraba su tramo.
        incidentes = self._publicar_incidentes() if len(eventos) < limite else 0
        return len(eventos) + incidentes

    def _publicar_incidentes(self) -> int:
        """Los incidentes se guardan en MongoDB con la marca pendiente_grafo=true en el MISMO
        documento (atómico). Acá se publican en el stream y se desmarcan: es el mismo patrón
        outbox, pero dentro de MongoDB."""
        if self._incidentes is None:
            return 0
        try:
            pendientes = self._incidentes.pendientes_grafo()
        except Exception as exc:  # noqa: BLE001   (MongoDB caído no frena al resto)
            log.warning("No se pudieron leer los incidentes pendientes de MongoDB: %s", exc)
            return 0
        if pendientes:
            self._cola.publicar([{"id_evento": f"inc-{p['id_incidente']}", "tipo_evento": "INCIDENTE_REGISTRADO",
                                  "payload": p} for p in pendientes])
            self._incidentes.marcar_publicados([p["id_incidente"] for p in pendientes])
        return len(pendientes)

    def procesar(self, proy: Proyeccion, cantidad: int = 100) -> int:
        if not proy.grupo_listo:
            proy.cola.asegurar_grupo()
            proy.grupo_listo = True
        proy.destino.verificar()         # si su motor no responde, ni se lee la cola (ErrorConexion)
        aplicados = 0
        for id_mensaje, campos in proy.cola.leer(proy.consumidor, cantidad):
            if campos is None:                       # el stream ya lo recortó: no hay nada que aplicar
                proy.cola.confirmar(id_mensaje)
                continue
            try:
                proy.destino.aplicar_evento(campos["tipo_evento"], json.loads(campos["payload"]),
                                            campos.get("id_evento"))
                proy.cola.confirmar(id_mensaje)
                aplicados += 1
            except Exception as exc:  # noqa: BLE001
                if es_transitorio(exc):
                    raise                    # el motor se cayó a mitad del ciclo: se reintenta después
                entregas = proy.cola.entregas(id_mensaje)
                if entregas >= MAX_ENTREGAS:
                    log.error("Evento %s (%s) falló %d veces en %s: pasa a sync:fallidos. %s",
                              campos.get("id_evento"), campos.get("tipo_evento"), entregas, proy.nombre, exc)
                    proy.cola.mover_a_fallidos(id_mensaje, campos, str(exc))
                    continue
                log.warning("No se pudo aplicar el evento %s en %s (intento %d): %s",
                            campos.get("id_evento"), proy.nombre, entregas, exc)
                break        # se corta para respetar el orden: los siguientes esperan al próximo ciclo
        if aplicados:
            log.info("Aplicados %d eventos en %s", aplicados, proy.nombre)
        return aplicados

    def procesar_cola(self, cantidad: int = 100) -> int:
        """Solo Neo4j (se mantiene por compatibilidad con la fase 2)."""
        return self.procesar(self.proyecciones[0], cantidad)

    def ciclo(self) -> tuple[int, int]:
        """Publica y después procesa CADA proyección por separado: si Neo4j está caído,
        MongoDB igual se actualiza (y al revés). Si alguna falló, se avisa al final."""
        publicados = self.publicar_pendientes()
        aplicados, error = 0, None
        for proy in self.proyecciones:
            try:
                aplicados += self.procesar(proy)
            except Exception as exc:  # noqa: BLE001
                if not es_transitorio(exc):
                    raise
                error = error or exc
        if error:
            raise error
        return publicados, aplicados

    # ------------------------------------------------------------------ estado
    def estado(self) -> dict:
        datos = {"outbox_pendientes": self._outbox.contar_pendientes(), **self._cola.estado()}
        datos["proyecciones"] = {}
        for proy in self.proyecciones:
            try:
                datos["proyecciones"][proy.nombre] = proy.cola.estado()["sin_confirmar"]
            except Exception:  # noqa: BLE001
                datos["proyecciones"][proy.nombre] = "?"
        try:
            datos["grafo"] = {f["etiqueta"]: f["cantidad"] for f in self._grafo.resumen()}
        except Exception as exc:  # noqa: BLE001
            datos["grafo"] = f"Neo4j no responde ({type(exc).__name__})"
        return datos

    # ------------------------------------------------------------------ reconstrucción
    def reconstruir_grafo(self) -> int:
        """Borra el grafo y lo vuelve a armar desde PostgreSQL (la fuente de verdad).
        Sirve si Neo4j se perdió o quedó desincronizado. Devuelve cuántos eventos aplicó."""
        self._grafo.vaciar()
        eventos: list[tuple[str, dict]] = []
        for u in self._catalogos.listar_ubicaciones():
            eventos.append(("UBICACION_GUARDADA", {"codigo": u.codigo, "nombre": u.nombre, "tipo": u.tipo,
                                                   "pais": u.pais, "latitud": u.latitud, "longitud": u.longitud}))
        for a in self._actores.listar():
            if a.tipo in ("CLIENTE", "OPERADOR"):
                eventos.append(("ACTOR_GUARDADO", {"id_actor": a.id_actor, "tipo": a.tipo, "estado": a.estado,
                                                   "razon_social": a.razon_social, "pais": a.pais}))
        for v in self._catalogos.listar_vehiculos():
            eventos.append(("VEHICULO_GUARDADO", {"id_vehiculo": v.id_vehiculo, "tipo": v.tipo,
                                                  "identificacion": v.identificacion, "id_operador": v.id_operador}))
        for k in self._contenedores.listar_contenedores():
            eventos.append(("CONTENEDOR_GUARDADO", {"id_contenedor": k.id_contenedor,
                                                    "codigo": k.codigo_internacional, "tipo": k.tipo}))
        envios, tramos = self._envios.todos_para_grafo()
        for e in envios:
            contenedores = e["contenedores"] if isinstance(e["contenedores"], list) else json.loads(e["contenedores"])
            eventos.append(("ENVIO_CREADO", {"id_envio": e["id_envio"], "id_cliente": e["id_cliente"],
                                             "estado": e["estado"], "prioridad": e["prioridad"],
                                             "origen": e["origen"], "destino": e["destino"],
                                             "fecha_creacion": e["fecha_creacion"].isoformat(),
                                             "contenedores": contenedores}))
        for t in tramos:
            eventos.append(("TRAMO_AGREGADO", {
                "id_tramo": t["id_tramo"], "id_envio": t["id_envio"], "orden": t["orden"],
                "medio": t["medio_transporte"], "codigo_origen": t["origen"], "codigo_destino": t["destino"],
                "id_operador": t["id_operador"], "id_vehiculo": t["id_vehiculo"], "costo": float(t["costo"]),
                "fecha_salida": t["fecha_salida"].isoformat(),
                "fecha_llegada_estimada": t["fecha_llegada_estimada"].isoformat()}))
            if t["fecha_llegada_real"] is not None:
                demora = (t["fecha_llegada_real"] - t["fecha_llegada_estimada"]).total_seconds() / 3600
                eventos.append(("TRAMO_LLEGADA", {"id_tramo": t["id_tramo"], "demora_h": round(demora, 2),
                                                  "fecha": t["fecha_llegada_real"].isoformat()}))
        if self._incidentes is not None:                       # fase 4: incidentes desde MongoDB
            for p in self._incidentes.todos_para_grafo():
                eventos.append(("INCIDENTE_REGISTRADO", p))
        for tipo, payload in eventos:
            self._grafo.aplicar_evento(tipo, payload)
        log.info("Grafo reconstruido desde PostgreSQL con %d eventos", len(eventos))
        return len(eventos)
