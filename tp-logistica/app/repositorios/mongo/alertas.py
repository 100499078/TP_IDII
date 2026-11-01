"""Alertas en MongoDB (colección `alertas`, con validador $jsonSchema).

Una alerta es un documento: según su origen tiene campos distintos (un sensor, un
evento...) y se consulta por estado y fecha. El validador de la colección
rechaza documentos mal formados aunque el código tuviera un error.
"""
from __future__ import annotations

from bson import ObjectId

from app.dominio.modelos import Alerta
from app.excepciones import ErrorConexion
from app.infra.conexiones import GestorConexiones


def _a_alerta(d: dict) -> Alerta:
    origen = d.get("origen", {})
    return Alerta(tipo=d["tipo"], fecha=d["fecha"], id_sensor=origen.get("id_sensor"),
                  id_contenedor=origen.get("id_contenedor"), mensaje=d.get("mensaje", ""), valor=d.get("valor"),
                  umbral=d.get("umbral"), id_envio=d.get("id_envio"), estado=d["estado"], id_alerta=str(d["_id"]))


class RepositorioAlertas:
    def __init__(self, conexiones: GestorConexiones):
        self._cx = conexiones

    @property
    def _col(self):
        return self._cx.mongo["alertas"]

    def _ejecutar(self, funcion):
        from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError
        try:
            return funcion()
        except (ServerSelectionTimeoutError, ConnectionFailure) as exc:
            raise ErrorConexion("MongoDB") from exc

    def insertar(self, a: Alerta) -> str:
        doc = {"tipo": a.tipo, "fecha": a.fecha, "estado": a.estado,
               "origen": {"id_sensor": a.id_sensor, "id_contenedor": a.id_contenedor},
               "id_envio": a.id_envio, "mensaje": a.mensaje[:500]}
        if a.valor is not None:
            doc["valor"] = float(a.valor)
        if a.umbral is not None:
            doc["umbral"] = float(a.umbral)
        a.id_alerta = str(self._ejecutar(lambda: self._col.insert_one(doc)).inserted_id)
        return a.id_alerta

    def listar(self, estados: list[str], limite: int = 50) -> list[Alerta]:
        cursor = self._col.find({"estado": {"$in": estados}}).sort("fecha", -1).limit(limite)   # ix_estado_fecha
        return [_a_alerta(d) for d in self._ejecutar(lambda: list(cursor))]

    def cambiar_estado(self, id_alerta: str, estado: str) -> bool:
        r = self._ejecutar(lambda: self._col.update_one({"_id": ObjectId(id_alerta)}, {"$set": {"estado": estado}}))
        return r.matched_count == 1

    def contar_por_estado(self) -> dict[str, int]:
        filas = self._ejecutar(lambda: list(self._col.aggregate([{"$group": {"_id": "$estado", "n": {"$sum": 1}}}])))
        return {f["_id"]: f["n"] for f in filas}
