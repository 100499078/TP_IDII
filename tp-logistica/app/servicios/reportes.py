"""Reportes asincrónicos (fase 7): se piden, se encolan en MongoDB y un proceso los genera.

Cada tipo de reporte sabe de qué motor sale:
    PostgreSQL: envíos por estado / país / cliente, demorados, tiempos de entrega (por ruta y por
                cliente), costos, ranking de clientes por volumen
    MongoDB   : incidentes abiertos por tipo
    Neo4j     : ranking de rutas (relación RUTA con sus contadores)
    Dos motores (fase 8): promedio de incidentes (envíos de PostgreSQL + incidentes de MongoDB)
                y desempeño histórico (meses de PostgreSQL + incidentes por mes de MongoDB);
                la "unión" entre motores se hace en Python, porque no hay JOIN entre bases.
El resultado se guarda en el mismo documento del pedido, convertido a tipos que MongoDB
acepta (Decimal -> float). Se puede exportar a CSV o JSON.
"""
from __future__ import annotations

import csv
import json
import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable

from app.excepciones import EntidadNoEncontrada, ErrorConexion, ErrorConflicto, ErrorValidacion

log = logging.getLogger(__name__)

TIPOS_REPORTE = {
    "ENVIOS_POR_ESTADO": "Envíos por estado",
    "ENVIOS_POR_PAIS": "Envíos por país de origen y destino",
    "ENVIOS_POR_CLIENTE": "Envíos por cliente",
    "ENVIOS_DEMORADOS": "Envíos demorados (horas de atraso)",
    "TIEMPOS_ENTREGA": "Tiempos de entrega por ruta",
    "TIEMPOS_POR_CLIENTE": "Tiempos mínimo, máximo y promedio por cliente",
    "COSTOS_TRANSPORTE": "Costos de transporte por medio",
    "RANKING_CLIENTES": "Ranking de clientes por volumen transportado",
    "INCIDENTES_ABIERTOS": "Incidentes abiertos por tipo",
    "PROMEDIO_INCIDENTES": "Promedio de incidentes por país, estado y cliente",
    "RANKING_RUTAS": "Rutas con más incidentes (grafo)",
    "DESEMPENO_HISTORICO": "Desempeño histórico mes a mes",
    "RIESGO_DEMORA": "Predicción de atrasos y riesgos (envíos en curso)",
}
# Fase 9: estos no se pueden pedir para un cliente (mostrarían datos de los demás)
SOLO_INTERNOS = {"RANKING_RUTAS"}
FORMATOS = ("PANTALLA", "CSV", "JSON")


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def promedio_incidentes(envios: list[dict], incidentes_por_envio: dict[int, int]) -> list[dict]:
    """Une los envíos (PostgreSQL) con la cantidad de incidentes de cada uno (MongoDB) y promedia
    por tres dimensiones. Es un "JOIN" hecho en la aplicación, por id_envio."""
    filas = []
    for dimension, clave in (("PAIS", lambda e: f"{e['pais_origen']} → {e['pais_destino']}"),
                             ("ESTADO", lambda e: e["estado"]), ("CLIENTE", lambda e: e["cliente"])):
        grupos: dict[str, list[int]] = {}
        for e in envios:
            grupos.setdefault(clave(e), []).append(incidentes_por_envio.get(e["id_envio"], 0))
        for valor, cantidades in sorted(grupos.items(), key=lambda g: -sum(g[1]) / len(g[1])):
            filas.append({"agrupado_por": dimension, "valor": valor, "envios": len(cantidades),
                          "incidentes": sum(cantidades), "promedio": round(sum(cantidades) / len(cantidades), 2)})
    return filas


def desempeno_historico(meses: list[dict], incidentes_por_mes: dict[str, dict]) -> list[dict]:
    """Agrega a cada mes de PostgreSQL los incidentes de ese mes (MongoDB) y cuántos hubo por envío."""
    filas = []
    for m in meses:
        inc = incidentes_por_mes.get(m["mes"], {})
        n = inc.get("incidentes", 0)
        filas.append({**m, "incidentes": n,
                      "incidentes_por_envio": round(n / m["envios"], 2) if m["envios"] else 0.0})
    return filas


def para_mongo(valor):
    """Decimal y date no se guardan tal cual en MongoDB: se convierten."""
    if isinstance(valor, Decimal):
        return float(valor)
    if isinstance(valor, datetime):
        return valor
    if isinstance(valor, date):
        return datetime(valor.year, valor.month, valor.day, tzinfo=timezone.utc)
    if isinstance(valor, dict):
        return {k: para_mongo(v) for k, v in valor.items()}
    if isinstance(valor, list):
        return [para_mongo(v) for v in valor]
    return valor


class ServicioReportes:
    def __init__(self, repo_reportes, repo_sql, repo_incidentes, repo_grafo, carpeta_exportacion: Path,
                 reloj: Callable[[], datetime] = _ahora, repo_predicciones=None):
        self._repo = repo_reportes
        self._carpeta = carpeta_exportacion
        self._reloj = reloj
        p = lambda f: (lambda prm: f(prm.get("desde"), prm.get("hasta"), prm.get("id_cliente")))  # noqa: E731
        self._generadores: dict[str, Callable[[dict], list[dict]]] = {
            "ENVIOS_POR_ESTADO": p(repo_sql.envios_por_estado),
            "ENVIOS_POR_PAIS": p(repo_sql.envios_por_pais),
            "ENVIOS_POR_CLIENTE": p(repo_sql.envios_por_cliente),
            "ENVIOS_DEMORADOS": p(repo_sql.envios_demorados),
            "TIEMPOS_ENTREGA": p(repo_sql.tiempos_entrega),
            "TIEMPOS_POR_CLIENTE": p(repo_sql.tiempos_por_cliente),
            "COSTOS_TRANSPORTE": p(repo_sql.costos_transporte),
            "RANKING_CLIENTES": p(repo_sql.ranking_clientes),
            "INCIDENTES_ABIERTOS": lambda prm: repo_incidentes.por_tipo(prm.get("id_cliente")),
            "PROMEDIO_INCIDENTES": self._promedio_incidentes,
            "RANKING_RUTAS": lambda prm: repo_grafo.rutas_problematicas(50),
            "DESEMPENO_HISTORICO": self._desempeno_historico,
            "RIESGO_DEMORA": self._riesgo,
        }
        self._sql, self._incidentes, self._predicciones = repo_sql, repo_incidentes, repo_predicciones

    # ------------------------------------------------------------------ reportes de dos motores
    def _promedio_incidentes(self, prm: dict) -> list[dict]:
        envios = self._sql.envios_para_incidentes(prm.get("desde"), prm.get("hasta"), prm.get("id_cliente"))
        conteo = self._incidentes.contar_por_envio([e["id_envio"] for e in envios])            # MongoDB
        return promedio_incidentes(envios, conteo)

    def _desempeno_historico(self, prm: dict) -> list[dict]:
        meses = self._sql.desempeno_mensual(prm.get("desde"), prm.get("hasta"), prm.get("id_cliente"))  # PostgreSQL
        return desempeno_historico(meses, self._incidentes.por_mes(id_cliente=prm.get("id_cliente")))   # MongoDB

    def _riesgo(self, prm: dict) -> list[dict]:
        """Fase 9: el reporte predictivo. Sale de la última predicción de cada envío (MongoDB); para un
        cliente, solo sus envíos (la lista sale de PostgreSQL)."""
        if self._predicciones is None:
            raise ErrorConflicto("La predicción de riesgo no está disponible")
        actuales = self._predicciones.actuales(500)
        if prm.get("id_cliente") is not None:
            suyos = {e["id_envio"] for e in self._sql.envios_para_incidentes(None, None, prm["id_cliente"])}
            actuales = [p for p in actuales if p["id_envio"] in suyos]
        return [{"id_envio": p["id_envio"], "nivel": p["nivel"], "probabilidad_demora": p["probabilidad_demora"],
                 "riesgo_operativo": p["riesgo_operativo"], "riesgo_climatico": p["riesgo_climatico"],
                 "ya_atrasado": p.get("variables", {}).get("ya_atrasado"),
                 "incidentes_abiertos": p.get("variables", {}).get("incidentes_abiertos"),
                 "calculado": p["fecha_calculo"], "modelo": f"{p['modelo']['nombre']} {p['modelo']['version']}"}
                for p in actuales]

    # ------------------------------------------------------------------ pedir y consultar
    def solicitar(self, id_usuario: int, tipo: str, desde: date | None = None, hasta: date | None = None,
                  formato: str = "PANTALLA", id_cliente: int | None = None) -> str:
        """id_cliente (fase 9): el reporte es de ese cliente (solo sus datos) y SE LE COBRA en su
        próxima factura. Un usuario de un cliente siempre pide con su propio id_cliente."""
        if tipo not in TIPOS_REPORTE:
            raise ErrorValidacion(f"Tipo inválido; opciones: {', '.join(TIPOS_REPORTE)}")
        if id_cliente is not None and tipo in SOLO_INTERNOS:
            raise ErrorValidacion(f"{TIPOS_REPORTE[tipo]} es un reporte interno: no se pide para un cliente")
        if formato not in FORMATOS:
            raise ErrorValidacion(f"Formato inválido; opciones: {', '.join(FORMATOS)}")
        if desde and hasta and desde > hasta:
            raise ErrorValidacion("La fecha 'desde' es posterior a 'hasta'")
        parametros = {k: datetime(v.year, v.month, v.day, tzinfo=timezone.utc)
                      for k, v in (("desde", desde), ("hasta", hasta)) if v}
        if id_cliente is None:
            return self._repo.solicitar(id_usuario, tipo, parametros, formato, self._reloj())
        return self._repo.solicitar(id_usuario, tipo, parametros, formato, self._reloj(), id_cliente=id_cliente)

    def mis_reportes(self, id_usuario: int) -> list[dict]:
        return self._repo.de_usuario(id_usuario)

    def obtener(self, id_reporte: str, id_usuario: int, es_supervisor: bool = False) -> dict:
        try:
            r = self._repo.obtener(id_reporte)
        except ErrorConexion:
            raise
        except Exception as exc:  # noqa: BLE001   (un ID con formato inválido)
            raise ErrorValidacion("El ID del reporte no es válido") from exc
        if r is None or (r["id_usuario"] != id_usuario and not es_supervisor):
            raise EntidadNoEncontrada("No existe el reporte (o no es tuyo)")
        return r

    # ------------------------------------------------------------------ generar (proceso)
    def procesar_pendientes(self, maximo: int = 20, id_reporte: str | None = None) -> str:
        """Toma pedidos de la cola uno por uno y los genera. Es el proceso periódico 'reportes'."""
        ahora = self._reloj()
        liberados = self._repo.liberar_trabados(ahora)
        generados = errores = 0
        for _ in range(maximo):
            pedido = self._repo.tomar_siguiente(self._reloj(), id_reporte)
            if pedido is None:
                break
            try:
                parametros = {k: v.date() for k, v in pedido.get("parametros", {}).items()}
                parametros["id_cliente"] = pedido.get("id_cliente")        # fase 9: solo sus datos
                filas = para_mongo(self._generadores[pedido["tipo"]](parametros))
                self._repo.guardar_resultado(pedido["id_reporte"], filas, self._reloj())
                generados += 1
            except Exception as exc:  # noqa: BLE001   (un reporte que falla no frena a los otros)
                log.warning("No se pudo generar el reporte %s (%s): %s", pedido["id_reporte"], pedido["tipo"], exc)
                self._repo.marcar_error(pedido["id_reporte"], f"{type(exc).__name__}: {exc}", self._reloj())
                errores += 1
            if id_reporte:
                break
        texto = f"{generados} reportes generados, {errores} con error"
        return texto + (f", {liberados} trabados devueltos a la cola" if liberados else "")

    # ------------------------------------------------------------------ exportar
    def exportar(self, reporte: dict, formato: str) -> Path:
        if reporte["estado"] != "GENERADO":
            raise ErrorConflicto(f"El reporte está {reporte['estado']}: todavía no tiene resultado")
        if formato not in ("CSV", "JSON"):
            raise ErrorValidacion("Se exporta a CSV o JSON")
        self._carpeta.mkdir(parents=True, exist_ok=True)
        ruta = self._carpeta / f"{reporte['tipo'].lower()}_{reporte['id_reporte'][-8:]}.{formato.lower()}"
        filas = reporte.get("resultado", [])
        if formato == "JSON":
            ruta.write_text(json.dumps(filas, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        else:
            columnas = list(dict.fromkeys(k for f in filas for k in f))
            with ruta.open("w", newline="", encoding="utf-8-sig") as archivo:   # -sig: Excel lee bien los acentos
                escritor = csv.DictWriter(archivo, fieldnames=columnas, delimiter=";")
                escritor.writeheader()
                for f in filas:
                    escritor.writerow({k: (", ".join(map(str, v)) if isinstance(v, list) else v) for k, v in f.items()})
        return ruta
