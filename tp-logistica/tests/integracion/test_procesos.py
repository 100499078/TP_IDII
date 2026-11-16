"""Integración de la fase 7.

- Cierre mensual contra PostgreSQL real (idempotente, con el CHECK del saldo final).
- Las consultas de los reportes de PostgreSQL corren sin error.
- Procesos con lock y estado en Redis real.
- Cola de reportes y predicción de riesgo con MongoDB real (se saltean si no está).
Borra lo que crea.
"""
import random
from datetime import datetime, timedelta, timezone

import pytest

from app.config import Config
from app.infra.conexiones import GestorConexiones
from app.repositorios.mongo.incidentes import RepositorioIncidentes
from app.repositorios.mongo.predicciones import RepositorioPredicciones
from app.repositorios.mongo.reportes import RepositorioReportes
from app.repositorios.neo4j.grafo import RepositorioGrafo
from app.repositorios.postgres.cierres import RepositorioCierres
from app.repositorios.postgres.reportes import RepositorioReportesSQL
from app.repositorios.redis.locks import RepositorioLocks
from app.repositorios.redis.procesos import RepositorioEstadoProcesos
from app.servicios.procesos import Proceso, ServicioProcesos
from app.servicios.reportes import ServicioReportes
from app.servicios.riesgo import ServicioRiesgo
from tests.integracion.test_operativa import cx, escenario  # noqa: F401  (fixtures)

pytestmark = pytest.mark.integracion
PERIODO = "2001-03"                    # un mes lejano que no existe en los datos reales


def test_cierre_mensual_idempotente(cx, escenario):  # noqa: F811
    cliente = escenario["cliente"]
    desde, hasta = datetime(2001, 3, 1, tzinfo=timezone.utc), datetime(2001, 4, 1, tzinfo=timezone.utc)
    try:
        with cx.transaccion() as cur:
            cur.execute("SELECT id_cuenta FROM cuenta_corriente WHERE id_actor = %s", (cliente,))
            cuenta = cur.fetchone()["id_cuenta"]
            for fecha, tipo, importe in [(datetime(2001, 2, 10, tzinfo=timezone.utc), "DEBITO", 500),   # antes del mes
                                         (datetime(2001, 3, 5, tzinfo=timezone.utc), "DEBITO", 1000),
                                         (datetime(2001, 3, 20, tzinfo=timezone.utc), "CREDITO", 300),
                                         (datetime(2001, 4, 2, tzinfo=timezone.utc), "CREDITO", 50)]:   # después
                cur.execute("INSERT INTO movimiento_cc (id_cuenta, fecha, tipo, importe, descripcion) "
                            "VALUES (%s, %s, %s, %s, 'prueba cierre')", (cuenta, fecha, tipo, importe))
        repo = RepositorioCierres(cx)
        assert repo.cerrar_periodo(PERIODO, desde, hasta) >= 1
        assert repo.cerrar_periodo(PERIODO, desde, hasta) == 0          # segunda vez: no hace nada
        c = next(x for x in repo.de_actor(cliente) if x["periodo"] == PERIODO)
        assert (c["saldo_inicial"], c["total_debitos"], c["total_creditos"], c["saldo_final"]) == (500, 1000, 300, 1200)
    finally:
        with cx.transaccion() as cur:
            cur.execute("DELETE FROM cierre_mensual WHERE periodo = %s", (PERIODO,))
            cur.execute("DELETE FROM movimiento_cc WHERE descripcion = 'prueba cierre'")


def test_consultas_de_reportes_en_postgres(cx):  # noqa: F811
    sql = RepositorioReportesSQL(cx)
    hoy = datetime.now(timezone.utc).date()
    for metodo in ("envios_por_estado", "envios_por_pais", "envios_por_cliente", "envios_demorados",
                   "tiempos_entrega", "costos_transporte", "ranking_clientes"):
        filas = getattr(sql, metodo)(hoy - timedelta(days=3650), hoy)
        assert isinstance(filas, list), metodo
    assert isinstance(sql.envios_en_curso(), list)


def test_procesos_con_lock_y_estado_en_redis(cx):  # noqa: F811
    nombre = f"prueba{random.randint(0, 10**6)}"
    locks, estado = RepositorioLocks(cx), RepositorioEstadoProcesos(cx)
    srv = ServicioProcesos([Proceso(nombre, "prueba", timedelta(minutes=5), lambda: "hecho")], estado, locks)
    try:
        token = locks.tomar(f"periodico:{nombre}", 30)          # "otro planificador" lo está corriendo
        assert "lock" in srv.ejecutar(nombre).resultado
        locks.soltar(f"periodico:{nombre}", token)
        assert srv.ejecutar(nombre).resultado == "hecho"
        assert estado.ultimo(nombre)["ok"] is True
        assert srv.pendientes() == []                           # acaba de correr: no le toca
    finally:
        cx.redis.delete(f"proceso:{nombre}", f"lock:proceso:periodico:{nombre}")


@pytest.fixture
def mongo(cx):  # noqa: F811
    try:
        cx.mongo.client.admin.command("ping")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"MongoDB no disponible: {exc}")
    return cx


def test_cola_de_reportes_real(mongo, tmp_path):
    cx = mongo
    repo = RepositorioReportes(cx)
    srv = ServicioReportes(repo, RepositorioReportesSQL(cx), RepositorioIncidentes(cx), RepositorioGrafo(cx), tmp_path)
    usuario = 900_000 + random.randint(0, 99_999)
    try:
        i = srv.solicitar(usuario, "ENVIOS_POR_ESTADO")
        ahora = datetime.now(timezone.utc)
        assert repo.tomar_siguiente(ahora, i)["estado"] == "EN_PROCESO"
        assert repo.tomar_siguiente(ahora, i) is None           # nadie más puede tomarlo
        cx.mongo["reportes"].update_one({"id_usuario": usuario}, {"$set": {"estado": "SOLICITADO"}})  # vuelve a la cola
        srv.procesar_pendientes(id_reporte=i)
        r = srv.obtener(i, usuario)
        assert r["estado"] == "GENERADO" and isinstance(r["resultado"], list)
        assert srv.exportar(r, "CSV").exists()
    finally:
        cx.mongo["reportes"].delete_many({"id_usuario": usuario})


def test_prediccion_de_riesgo_real(mongo):
    cx = mongo
    srv = ServicioRiesgo(RepositorioReportesSQL(cx), RepositorioGrafo(cx), RepositorioIncidentes(cx),
                         RepositorioPredicciones(cx))
    antes = datetime.now(timezone.utc)
    texto = srv.calcular_todos()                                # funciona aunque Neo4j esté caído
    assert "evaluados" in texto or "no hay envíos" in texto
    for p in srv.actuales(50):
        assert 0 <= p["probabilidad_demora"] <= 1 and p["nivel"] in ("BAJO", "MEDIO", "ALTO", "CRITICO")
    cx.mongo["predicciones_riesgo"].delete_many({"fecha_calculo": {"$gte": antes}})
