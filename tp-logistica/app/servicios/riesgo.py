"""Predicción de riesgo de demora de los envíos en curso (proceso periódico, fase 7).

No es aprendizaje automático: es una HEURÍSTICA (una fórmula con pesos) que usa datos reales
de cuatro motores:

    PostgreSQL -> envíos en curso, sus tramos pendientes, la fecha de entrega comprometida
    Neo4j      -> por cada tramo pendiente, la demora promedio y la tasa de incidentes de su RUTA
    MongoDB    -> incidentes abiertos del envío
    MongoDB    <- se guarda una predicción nueva por envío (colección predicciones_riesgo)

    riesgo_operativo   = 0.5 · tasa de incidentes de la peor ruta + 0.15 por incidente abierto
                         (+ 0.3 si el envío ya está DEMORADO)                  -> tope 1
    riesgo_climatico   = base del medio (marítimo 0.3, aéreo 0.15, terrestre 0.1)
                         (+ 0.2 si hay un incidente de CLIMA abierto)           -> tope 1
    probabilidad_demora = 1 si ya pasó la fecha comprometida; si no:
                         0.4 · operativo + 0.3 · climático + 0.3 · (demora prom. / 24 h)
                         (+ 0.3 si lo planificado ya llega después de lo comprometido) -> tope 1
    nivel: < 0.25 BAJO · < 0.5 MEDIO · < 0.75 ALTO · resto CRITICO

Si Neo4j no responde, se calcula igual sin los datos de rutas y se anota en `variables`.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable

log = logging.getLogger(__name__)

MODELO = {"nombre": "heuristica-demora", "version": "1.0"}
RIESGO_MEDIO = {"MARITIMO": 0.3, "AEREO": 0.15, "TERRESTRE": 0.1}


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def nivel_de(probabilidad: float) -> str:
    if probabilidad < 0.25:
        return "BAJO"
    if probabilidad < 0.5:
        return "MEDIO"
    if probabilidad < 0.75:
        return "ALTO"
    return "CRITICO"


def calcular(envio: dict, rutas: dict[tuple, dict], incidentes: dict | None, ahora: datetime) -> dict:
    """La fórmula, sin acceso a bases: recibe los datos ya leídos y devuelve el documento."""
    pendientes = envio["tramos_pendientes"] or []
    stats = [rutas.get((t["origen"], t["destino"], t["medio"])) for t in pendientes]
    stats = [s for s in stats if s]
    tasa = max(((s["incidentes"] or 0) / s["envios"] for s in stats if s["envios"]), default=0.0)
    demora = max((s["demora_h"] or 0.0 for s in stats), default=0.0)
    abiertos = (incidentes or {}).get("abiertos", 0)
    tipos = (incidentes or {}).get("tipos", [])

    operativo = min(1.0, 0.5 * tasa + 0.15 * abiertos + (0.3 if envio["estado"] == "DEMORADO" else 0.0))
    climatico = min(1.0, max((RIESGO_MEDIO.get(t["medio"], 0.1) for t in pendientes), default=0.0)
                    + (0.2 if "CLIMA" in tipos else 0.0))
    atrasado = ahora > envio["fecha_entrega_estimada"]
    llega_tarde = bool(envio["llegada_planificada"]) and envio["llegada_planificada"] > envio["fecha_entrega_estimada"]
    if atrasado:
        probabilidad = 1.0
    else:
        probabilidad = min(1.0, 0.4 * operativo + 0.3 * climatico + 0.3 * min(1.0, max(demora, 0.0) / 24)
                           + (0.3 if llega_tarde else 0.0))
    return {"id_envio": envio["id_envio"], "fecha_calculo": ahora,
            "probabilidad_demora": round(float(probabilidad), 3), "riesgo_climatico": round(float(climatico), 3),
            "riesgo_operativo": round(float(operativo), 3), "nivel": nivel_de(probabilidad), "modelo": MODELO,
            "variables": {"tramos_pendientes": len(pendientes), "tasa_incidentes_ruta": round(tasa, 3),
                          "demora_prom_ruta_h": round(demora, 2), "incidentes_abiertos": abiertos,
                          "ya_atrasado": atrasado, "plan_llega_tarde": llega_tarde}}


class ServicioRiesgo:
    def __init__(self, repo_reportes_sql, repo_grafo, repo_incidentes, repo_predicciones,
                 reloj: Callable[[], datetime] = _ahora):
        self._sql = repo_reportes_sql
        self._grafo = repo_grafo
        self._incidentes = repo_incidentes
        self._predicciones = repo_predicciones
        self._reloj = reloj

    def calcular_todos(self) -> str:
        """El proceso periódico: calcula y guarda una predicción por envío en curso."""
        ahora = self._reloj()
        envios = self._sql.envios_en_curso()
        if not envios:
            return "no hay envíos en curso"
        rutas_pedidas = [{"origen": t["origen"], "destino": t["destino"], "medio": t["medio"]}
                         for e in envios for t in (e["tramos_pendientes"] or [])]
        sin_grafo = False
        try:
            rutas = self._grafo.estadisticas_rutas(rutas_pedidas)
        except Exception as exc:  # noqa: BLE001   (Neo4j caído: se predice con menos datos)
            log.warning("Predicción sin datos de rutas (Neo4j no responde): %s", exc)
            rutas, sin_grafo = {}, True
        incidentes = self._incidentes.abiertos_por_envio([e["id_envio"] for e in envios])
        documentos = []
        for e in envios:
            doc = calcular(e, rutas, incidentes.get(e["id_envio"]), ahora)
            doc["variables"]["sin_datos_de_rutas"] = sin_grafo
            documentos.append(doc)
        self._predicciones.guardar(documentos)
        altos = sum(1 for d in documentos if d["nivel"] in ("ALTO", "CRITICO"))
        return f"{len(documentos)} envíos evaluados, {altos} con riesgo ALTO o CRITICO"

    def actuales(self, limite: int = 20) -> list[dict]:
        return self._predicciones.actuales(limite)

    def historia(self, id_envio: int) -> list[dict]:
        return self._predicciones.historia(id_envio)
