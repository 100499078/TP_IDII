"""Predicciones de riesgo en MongoDB (colección `predicciones_riesgo`).

Se guarda una predicción NUEVA por envío en cada corrida (no se pisa la anterior): así queda
la historia de cómo cambió el riesgo. Para "el riesgo actual" se toma la más reciente de cada
envío con una agregación ($sort + $group con $first).
"""
from __future__ import annotations

from datetime import datetime

from app.excepciones import ErrorConexion
from app.infra.conexiones import GestorConexiones


def _ejecutar(funcion):
    from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError
    try:
        return funcion()
    except (ServerSelectionTimeoutError, ConnectionFailure) as exc:
        raise ErrorConexion("MongoDB") from exc


class RepositorioPredicciones:
    def __init__(self, conexiones: GestorConexiones):
        self._cx = conexiones

    @property
    def _col(self):
        return self._cx.mongo["predicciones_riesgo"]

    def guardar(self, predicciones: list[dict]) -> int:
        if not predicciones:
            return 0
        return len(_ejecutar(lambda: self._col.insert_many(predicciones)).inserted_ids)

    def actuales(self, limite: int = 20, desde: datetime | None = None) -> list[dict]:
        """La predicción más reciente de cada envío, de mayor a menor probabilidad de demora."""
        pipeline = []
        if desde:
            pipeline.append({"$match": {"fecha_calculo": {"$gte": desde}}})
        pipeline += [
            {"$sort": {"id_envio": 1, "fecha_calculo": -1}},                 # ix_envio_fecha
            {"$group": {"_id": "$id_envio", "ultima": {"$first": "$$ROOT"}}},
            {"$replaceRoot": {"newRoot": "$ultima"}},
            {"$sort": {"probabilidad_demora": -1}},
            {"$limit": limite},
            {"$project": {"_id": 0}},
        ]
        return _ejecutar(lambda: list(self._col.aggregate(pipeline)))

    def historia(self, id_envio: int, limite: int = 10) -> list[dict]:
        cursor = self._col.find({"id_envio": id_envio}, {"_id": 0}).sort("fecha_calculo", -1).limit(limite)
        return _ejecutar(lambda: list(cursor))
