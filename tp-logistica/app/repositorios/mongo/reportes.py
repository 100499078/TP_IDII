"""Reportes en MongoDB (colección `reportes`): funciona como una COLA DE TRABAJOS.

    SOLICITADO --(un proceso lo toma)--> EN_PROCESO --> GENERADO (con `resultado`)
                                                     \\-> ERROR    (con `error`)

Tomar un pedido es find_one_and_update({estado: SOLICITADO} -> EN_PROCESO): MongoDB lo hace
de forma atómica sobre un documento, así que dos procesos nunca toman el mismo reporte.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from bson import ObjectId
from pymongo import ReturnDocument

from app.excepciones import ErrorConexion
from app.infra.conexiones import GestorConexiones


def _ejecutar(funcion):
    from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError
    try:
        return funcion()
    except (ServerSelectionTimeoutError, ConnectionFailure) as exc:
        raise ErrorConexion("MongoDB") from exc


def _limpio(d: dict | None) -> dict | None:
    if d is not None:
        d["id_reporte"] = str(d.pop("_id"))
    return d


class RepositorioReportes:
    def __init__(self, conexiones: GestorConexiones):
        self._cx = conexiones

    @property
    def _col(self):
        return self._cx.mongo["reportes"]

    def solicitar(self, id_usuario: int, tipo: str, parametros: dict, formato: str, fecha: datetime,
                  id_cliente: int | None = None) -> str:
        doc = {"id_usuario": id_usuario, "tipo": tipo, "parametros": parametros, "formato": formato,
               "estado": "SOLICITADO", "fecha_solicitud": fecha}
        if id_cliente is not None:                        # fase 9: reporte de un cliente, se le cobra
            doc.update(id_cliente=id_cliente, facturable=True)
        r = _ejecutar(lambda: self._col.insert_one(doc))
        return str(r.inserted_id)

    def tomar_siguiente(self, ahora: datetime, id_reporte: str | None = None) -> dict | None:
        """El pedido más viejo en SOLICITADO pasa a EN_PROCESO, en una sola operación (índice ix_cola)."""
        filtro = {"estado": "SOLICITADO"}
        if id_reporte:
            filtro["_id"] = ObjectId(id_reporte)
        return _limpio(_ejecutar(lambda: self._col.find_one_and_update(
            filtro, {"$set": {"estado": "EN_PROCESO", "inicio_proceso": ahora}},
            sort=[("fecha_solicitud", 1)], return_document=ReturnDocument.AFTER)))

    def guardar_resultado(self, id_reporte: str, filas: list[dict], ahora: datetime) -> None:
        _ejecutar(lambda: self._col.update_one({"_id": ObjectId(id_reporte)},
                                               {"$set": {"estado": "GENERADO", "resultado": filas,
                                                         "fecha_generacion": ahora}}))

    def marcar_error(self, id_reporte: str, error: str, ahora: datetime) -> None:
        _ejecutar(lambda: self._col.update_one({"_id": ObjectId(id_reporte)},
                                               {"$set": {"estado": "ERROR", "error": error[:500],
                                                         "fecha_generacion": ahora}}))

    def liberar_trabados(self, ahora: datetime, minutos: int = 10) -> int:
        """Si un proceso se cayó con un reporte EN_PROCESO, después de `minutos` vuelve a la cola."""
        r = _ejecutar(lambda: self._col.update_many(
            {"estado": "EN_PROCESO", "inicio_proceso": {"$lt": ahora - timedelta(minutes=minutos)}},
            {"$set": {"estado": "SOLICITADO"}}))
        return r.modified_count

    def de_usuario(self, id_usuario: int, limite: int = 20) -> list[dict]:
        cursor = (self._col.find({"id_usuario": id_usuario}, {"resultado": 0})        # sin el resultado: es pesado
                  .sort("fecha_solicitud", -1).limit(limite))                         # ix_usuario_fecha
        return [_limpio(d) for d in _ejecutar(lambda: list(cursor))]

    def obtener(self, id_reporte: str) -> dict | None:
        return _limpio(_ejecutar(lambda: self._col.find_one({"_id": ObjectId(id_reporte)})))

    # ------------------------------------------------------------------ facturación (fase 9)
    def facturables(self, id_cliente: int | None = None) -> list[dict]:
        """Reportes GENERADOS para un cliente que todavía no se le cobraron (sin id_factura)."""
        filtro = {"facturable": True, "estado": "GENERADO", "id_factura": {"$exists": False}}
        if id_cliente is not None:
            filtro["id_cliente"] = id_cliente
        cursor = self._col.find(filtro, {"resultado": 0}).sort("fecha_solicitud", 1)
        return [_limpio(d) for d in _ejecutar(lambda: list(cursor))]

    def clientes_con_facturables(self) -> list[int]:
        return sorted(_ejecutar(lambda: self._col.distinct(
            "id_cliente", {"facturable": True, "estado": "GENERADO", "id_factura": {"$exists": False}})))

    def marcar_facturados(self, ids: list[str], id_factura: int, numero: str) -> None:
        _ejecutar(lambda: self._col.update_many({"_id": {"$in": [ObjectId(i) for i in ids]}},
                                                {"$set": {"id_factura": id_factura, "numero_factura": numero}}))

    def desmarcar_facturados(self, ids: list[str]) -> None:
        """Al anular la factura, los reportes vuelven a quedar para cobrar."""
        _ejecutar(lambda: self._col.update_many({"_id": {"$in": [ObjectId(i) for i in ids]}},
                                                {"$unset": {"id_factura": "", "numero_factura": ""}}))

    def ids_facturados(self) -> set[str]:
        """Para el verificador: los reportes que MongoDB tiene marcados como cobrados."""
        return {str(d["_id"]) for d in _ejecutar(lambda: list(self._col.find({"id_factura": {"$exists": True}},
                                                                            {"_id": 1})))}
