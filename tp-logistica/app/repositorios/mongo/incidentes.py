"""Incidentes en MongoDB (colección `incidentes`).

- El documento EMBEBE su contexto (cliente, países, ruta, tramo, medio): los rankings y
  promedios se calculan con una agregación sobre una sola colección, sin JOIN.
- El historial de estados va embebido como arreglo ($push).
- `pendiente_grafo: true` es un "outbox" dentro del mismo documento: en MongoDB la escritura
  de UN documento es atómica, así que el incidente y la marca se guardan juntos. El
  sincronizador lo publica después hacia Neo4j y la borra.
"""
from __future__ import annotations

from datetime import datetime

from bson import ObjectId

from app.dominio.modelos import Incidente
from app.excepciones import ErrorConexion
from app.infra.conexiones import GestorConexiones


def _a_incidente(d: dict) -> Incidente:
    c = d["contexto"]
    return Incidente(id_envio=d["id_envio"], tipo=d["tipo"], severidad=d["severidad"], fecha=d["fecha"],
                     id_cliente=c["id_cliente"], pais_origen=c["pais_origen"], pais_destino=c["pais_destino"],
                     ruta=c["ruta"], descripcion=d.get("descripcion", ""), id_tramo=c.get("id_tramo"),
                     medio=c.get("medio"), estado=d["estado"], historial=d.get("historial_estados", []),
                     id_incidente=str(d["_id"]))


def _payload_grafo(d: dict) -> dict:
    return {"id_incidente": str(d["_id"]), "id_envio": d["id_envio"], "id_tramo": d["contexto"].get("id_tramo"),
            "tipo": d["tipo"], "severidad": d["severidad"], "fecha": d["fecha"].isoformat()}


class RepositorioIncidentes:
    def __init__(self, conexiones: GestorConexiones):
        self._cx = conexiones
        self._indices_listos = False

    @property
    def _col(self):
        col = self._cx.mongo["incidentes"]
        if not self._indices_listos:
            self._ejecutar(lambda: col.create_index("pendiente_grafo", name="ix_pendiente_grafo",
                                                    partialFilterExpression={"pendiente_grafo": True}))
            self._indices_listos = True
        return col

    def _ejecutar(self, funcion):
        from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError
        try:
            return funcion()
        except (ServerSelectionTimeoutError, ConnectionFailure) as exc:
            raise ErrorConexion("MongoDB") from exc

    # ------------------------------------------------------------------ escritura
    def insertar(self, i: Incidente, id_usuario: int | None) -> str:
        contexto = {"id_cliente": i.id_cliente, "pais_origen": i.pais_origen, "pais_destino": i.pais_destino,
                    "ruta": i.ruta}
        if i.id_tramo is not None:                    # el validador no acepta null en estos campos
            contexto["id_tramo"] = i.id_tramo
        if i.medio:
            contexto["medio"] = i.medio
        doc = {"id_envio": i.id_envio, "tipo": i.tipo, "fecha": i.fecha, "severidad": i.severidad,
               "estado": i.estado, "descripcion": i.descripcion[:1000], "contexto": contexto,
               "historial_estados": [{"estado": i.estado, "fecha": i.fecha, "id_usuario": id_usuario}],
               "pendiente_grafo": True}
        i.id_incidente = str(self._ejecutar(lambda: self._col.insert_one(doc)).inserted_id)
        return i.id_incidente

    def cambiar_estado(self, id_incidente: str, estado: str, fecha: datetime, id_usuario: int | None) -> bool:
        r = self._ejecutar(lambda: self._col.update_one(
            {"_id": ObjectId(id_incidente)},
            {"$set": {"estado": estado},
             "$push": {"historial_estados": {"estado": estado, "fecha": fecha, "id_usuario": id_usuario}}}))
        return r.matched_count == 1

    # ------------------------------------------------------------------ consultas
    def obtener(self, id_incidente: str) -> Incidente | None:
        d = self._ejecutar(lambda: self._col.find_one({"_id": ObjectId(id_incidente)}))
        return _a_incidente(d) if d else None

    def listar(self, estados: list[str], limite: int = 50) -> list[Incidente]:
        cursor = (self._col.find({"estado": {"$in": estados}})
                  .sort([("severidad", -1), ("fecha", -1)]).limit(limite))           # ix_estado_severidad
        return [_a_incidente(d) for d in self._ejecutar(lambda: list(cursor))]

    def de_envio(self, id_envio: int) -> list[Incidente]:
        cursor = self._col.find({"id_envio": id_envio}).sort("fecha", 1)              # ix_envio
        return [_a_incidente(d) for d in self._ejecutar(lambda: list(cursor))]

    def ranking_rutas(self, limite: int = 10) -> list[dict]:
        """Agregación: por cada ruta (origen-destino del envío), cuántos incidentes hubo,
        cuántos siguen abiertos, la severidad promedio y los tipos que aparecieron."""
        pipeline = [
            {"$group": {"_id": "$contexto.ruta",
                        "incidentes": {"$sum": 1},
                        "abiertos": {"$sum": {"$cond": [{"$in": ["$estado", ["ABIERTO", "EN_ANALISIS"]]}, 1, 0]}},
                        "severidad_prom": {"$avg": "$severidad"},
                        "tipos": {"$addToSet": "$tipo"}}},
            {"$sort": {"incidentes": -1, "severidad_prom": -1}},
            {"$limit": limite},
            {"$project": {"_id": 0, "ruta": "$_id", "incidentes": 1, "abiertos": 1,
                          "severidad_prom": {"$round": ["$severidad_prom", 1]}, "tipos": 1}},
        ]
        return self._ejecutar(lambda: list(self._col.aggregate(pipeline)))

    def abiertos_por_envio(self, ids_envio: list[int]) -> dict[int, dict]:
        """{id_envio: {"abiertos": n, "tipos": [...]}} para la predicción de riesgo (una sola consulta)."""
        if not ids_envio:
            return {}
        pipeline = [{"$match": {"id_envio": {"$in": ids_envio}, "estado": {"$in": ["ABIERTO", "EN_ANALISIS"]}}},
                    {"$group": {"_id": "$id_envio", "abiertos": {"$sum": 1}, "tipos": {"$addToSet": "$tipo"},
                                "severidad_max": {"$max": "$severidad"}}}]
        return {f["_id"]: f for f in self._ejecutar(lambda: list(self._col.aggregate(pipeline)))}

    def por_tipo(self, id_cliente: int | None = None) -> list[dict]:
        """Para el reporte INCIDENTES_ABIERTOS: cantidad y severidad promedio por tipo, solo abiertos.
        Fase 9: id_cliente filtra por el contexto embebido (contexto.id_cliente)."""
        filtro = {"estado": {"$in": ["ABIERTO", "EN_ANALISIS"]}}
        if id_cliente is not None:
            filtro["contexto.id_cliente"] = id_cliente
        pipeline = [{"$match": filtro},
                    {"$group": {"_id": "$tipo", "abiertos": {"$sum": 1}, "severidad_prom": {"$avg": "$severidad"}}},
                    {"$sort": {"abiertos": -1}},
                    {"$project": {"_id": 0, "tipo": "$_id", "abiertos": 1, "severidad_prom": 1}}]
        return self._ejecutar(lambda: list(self._col.aggregate(pipeline)))

    def contar_por_envio(self, ids_envio: list[int]) -> dict[int, int]:
        """{id_envio: cantidad de incidentes} (todos los estados). Fase 8: reporte PROMEDIO_INCIDENTES,
        que combina este conteo de MongoDB con los envíos de PostgreSQL."""
        if not ids_envio:
            return {}
        pipeline = [{"$match": {"id_envio": {"$in": ids_envio}}},
                    {"$group": {"_id": "$id_envio", "n": {"$sum": 1}}}]
        return {f["_id"]: f["n"] for f in self._ejecutar(lambda: list(self._col.aggregate(pipeline)))}

    def por_mes(self, desde: datetime | None = None, hasta: datetime | None = None,
                id_cliente: int | None = None) -> dict[str, dict]:
        """{"2026-09": {"incidentes": n, "severidad_prom": x}} para el desempeño histórico (fase 8)."""
        filtro = {} if id_cliente is None else {"contexto.id_cliente": id_cliente}
        if desde or hasta:
            filtro["fecha"] = {k: v for k, v in (("$gte", desde), ("$lt", hasta)) if v}
        pipeline = [{"$match": filtro},
                    {"$group": {"_id": {"$dateToString": {"format": "%Y-%m", "date": "$fecha"}},
                                "incidentes": {"$sum": 1}, "severidad_prom": {"$avg": "$severidad"}}}]
        return {f["_id"]: f for f in self._ejecutar(lambda: list(self._col.aggregate(pipeline)))}

    def contar(self) -> int:
        return self._ejecutar(lambda: self._col.count_documents({}))

    def contar_pendientes_grafo(self) -> int:
        return self._ejecutar(lambda: self._col.count_documents({"pendiente_grafo": True}))

    # ------------------------------------------------------------------ "outbox" hacia Neo4j
    def pendientes_grafo(self, limite: int = 200) -> list[dict]:
        cursor = self._col.find({"pendiente_grafo": True}).sort("_id", 1).limit(limite)
        return [_payload_grafo(d) for d in self._ejecutar(lambda: list(cursor))]

    def marcar_publicados(self, ids: list[str]) -> None:
        self._ejecutar(lambda: self._col.update_many({"_id": {"$in": [ObjectId(i) for i in ids]}},
                                                     {"$unset": {"pendiente_grafo": ""}}))

    def todos_para_grafo(self) -> list[dict]:
        """Para reconstruir el grafo desde cero."""
        return [_payload_grafo(d) for d in self._ejecutar(lambda: list(self._col.find().sort("_id", 1)))]
