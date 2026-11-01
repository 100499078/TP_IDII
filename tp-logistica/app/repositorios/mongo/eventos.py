"""Eventos logísticos en MongoDB (colección `eventos_logisticos`): la línea de tiempo de cada envío.

- Los genera el sistema (desde el stream de sincronización, grupo "trazabilidad") o un operador.
- Los del sistema llevan `id_evento_origen` (el id del outbox de PostgreSQL) con índice ÚNICO:
  si el mismo evento llega dos veces, el upsert con $setOnInsert no lo duplica (idempotencia).
- La ubicación es un punto GeoJSON con índice 2dsphere: permite buscar "cerca de".
"""
from __future__ import annotations

from app.dominio.modelos import EventoLogistico
from app.excepciones import ErrorConexion
from app.infra.conexiones import GestorConexiones


def _a_evento(d: dict) -> EventoLogistico:
    u = d["ubicacion"]
    lon, lat = u["punto"]["coordinates"]
    reg = d.get("registrado_por") or {}
    return EventoLogistico(id_envio=d["id_envio"], tipo=d["tipo"], fecha_hora=d["fecha_hora"],
                           codigo_ubicacion=u["codigo"], pais=u["pais"], latitud=lat, longitud=lon,
                           nombre_ubicacion=u.get("nombre"), descripcion=d.get("descripcion"),
                           detalle=d.get("detalle", {}), id_usuario=reg.get("id_usuario"),
                           nombre_usuario=reg.get("nombre"), id_evento=str(d["_id"]))


class RepositorioEventos:
    def __init__(self, conexiones: GestorConexiones):
        self._cx = conexiones
        self._indices_listos = False

    @property
    def _col(self):
        col = self._cx.mongo["eventos_logisticos"]
        if not self._indices_listos:
            # create_index es idempotente: si ya existe, no hace nada
            self._ejecutar(lambda: col.create_index(
                "id_evento_origen", name="ux_evento_origen", unique=True,
                partialFilterExpression={"id_evento_origen": {"$exists": True}}))
            self._indices_listos = True
        return col

    def _ejecutar(self, funcion):
        from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError
        try:
            return funcion()
        except (ServerSelectionTimeoutError, ConnectionFailure) as exc:
            raise ErrorConexion("MongoDB") from exc

    def verificar(self) -> None:
        self._ejecutar(lambda: self._cx.mongo.client.admin.command("ping"))

    @staticmethod
    def _documento(e: EventoLogistico) -> dict:
        doc = {"id_envio": e.id_envio, "tipo": e.tipo, "fecha_hora": e.fecha_hora,
               "ubicacion": {"codigo": e.codigo_ubicacion, "pais": e.pais,
                             # GeoJSON: primero la LONGITUD, después la latitud
                             "punto": {"type": "Point", "coordinates": [float(e.longitud), float(e.latitud)]}}}
        if e.nombre_ubicacion:
            doc["ubicacion"]["nombre"] = e.nombre_ubicacion
        if e.descripcion:
            doc["descripcion"] = e.descripcion[:500]
        if e.detalle:
            doc["detalle"] = e.detalle
        if e.id_usuario is not None:
            doc["registrado_por"] = {"id_usuario": e.id_usuario, "nombre": e.nombre_usuario or ""}
        return doc

    def registrar(self, e: EventoLogistico, id_evento_origen: int | None = None) -> None:
        doc = self._documento(e)
        if id_evento_origen is None:
            e.id_evento = str(self._ejecutar(lambda: self._col.insert_one(doc)).inserted_id)
            return
        doc["id_evento_origen"] = id_evento_origen
        # upsert + $setOnInsert: inserta la primera vez; si ya estaba, no cambia nada
        self._ejecutar(lambda: self._col.update_one({"id_evento_origen": id_evento_origen},
                                                    {"$setOnInsert": doc}, upsert=True))

    def de_envio(self, id_envio: int) -> list[EventoLogistico]:
        cursor = self._col.find({"id_envio": id_envio}).sort("fecha_hora", 1)         # ix_envio_fecha
        return [_a_evento(d) for d in self._ejecutar(lambda: list(cursor))]

    def envios_con_evento(self, tipo: str) -> set[int]:
        """Los envíos que tienen al menos un evento de ese tipo (para el verificador de la fase 8)."""
        return set(self._ejecutar(lambda: self._col.distinct("id_envio", {"tipo": tipo})))

    def cerca_de(self, latitud: float, longitud: float, km: float, limite: int = 50) -> list[EventoLogistico]:
        """Eventos a menos de `km` kilómetros de un punto, del más cercano al más lejano (índice 2dsphere)."""
        filtro = {"ubicacion.punto": {"$nearSphere": {
            "$geometry": {"type": "Point", "coordinates": [longitud, latitud]},
            "$maxDistance": km * 1000}}}                                              # en metros
        cursor = self._col.find(filtro).limit(limite)
        return [_a_evento(d) for d in self._ejecutar(lambda: list(cursor))]
