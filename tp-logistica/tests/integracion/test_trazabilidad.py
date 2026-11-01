"""Integración de la fase 4 contra PostgreSQL, MongoDB y Redis reales.

Arma un envío real, pasa sus eventos del outbox por la proyección de trazabilidad hacia
MongoDB (dos veces, para comprobar que no se duplican), registra un incidente, usa la
caché del ranking y la búsqueda geoespacial. Al final borra todo lo que creó.
"""
import random
from datetime import datetime, timedelta, timezone

import pytest

from app.dominio.modelos import Sesion
from app.repositorios.mongo.eventos import RepositorioEventos
from app.repositorios.mongo.incidentes import RepositorioIncidentes
from app.repositorios.postgres.actores import RepositorioActores
from app.repositorios.postgres.catalogos import RepositorioCatalogos
from app.repositorios.postgres.contenedores import RepositorioContenedores
from app.repositorios.postgres.envios import RepositorioEnvios
from app.repositorios.redis.cache import RepositorioCache
from app.servicios.envios import ServicioEnvios
from app.servicios.trazabilidad import CLAVE_RANKING, ProyeccionTrazabilidad, ServicioTrazabilidad
from tests.integracion.test_operativa import cx, escenario, eventos_nuevos  # noqa: F401  (fixtures)

pytestmark = pytest.mark.integracion


@pytest.fixture
def mongo(cx):  # noqa: F811
    try:
        cx.mongo.client.admin.command("ping")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"MongoDB no disponible: {exc}")
    return cx


def test_linea_de_tiempo_incidente_ranking_y_geo(mongo, escenario, monkeypatch):  # noqa: F811
    cx = mongo
    envios, catalogos = RepositorioEnvios(cx), RepositorioCatalogos(cx)
    srv = ServicioEnvios(envios, RepositorioActores(cx), catalogos, RepositorioContenedores(cx))
    ahora = datetime.now(timezone.utc).replace(microsecond=0)
    e = srv.crear_envio(escenario["cliente"], "ARROS", "NLRTM", ahora + timedelta(days=20), 500, 30,
                        [escenario["contenedor"]])
    t1 = srv.agregar_tramo(e, "TERRESTRE", "ARBUE", escenario["operador"], ahora + timedelta(minutes=1),
                           ahora + timedelta(hours=6))
    srv.agregar_tramo(e, "MARITIMO", "NLRTM", escenario["operador"], ahora + timedelta(hours=7),
                      ahora + timedelta(days=15))
    srv.cambiar_estado(e, "EN_TRANSITO")
    srv.registrar_llegada(t1, ahora + timedelta(minutes=2))

    repo_eventos, repo_incidentes = RepositorioEventos(cx), RepositorioIncidentes(cx)
    clave = f"test:{CLAVE_RANKING}:{random.randint(0, 10**9)}"
    monkeypatch.setattr("app.servicios.trazabilidad.CLAVE_RANKING", clave)
    try:
        # 1) outbox -> MongoDB, aplicado DOS veces: no se duplica (índice único + $setOnInsert)
        proyeccion = ProyeccionTrazabilidad(repo_eventos, envios, catalogos)
        for _ in range(2):
            for ev in eventos_nuevos(cx, escenario["ultimo_evento"]):
                proyeccion.aplicar_evento(ev["tipo_evento"], ev["payload"], str(ev["id_evento"]))
        linea = repo_eventos.de_envio(e)
        assert [x.tipo for x in linea] == ["CARGA_INICIADA", "EN_TRANSITO", "DESCARGA"]
        assert linea[-1].codigo_ubicacion == "ARBUE"

        # 2) incidente con contexto embebido y marca para el grafo
        traz = ServicioTrazabilidad(envios, catalogos, repo_eventos, repo_incidentes, RepositorioCache(cx),
                                    reloj=lambda: ahora + timedelta(minutes=3))
        sesion = Sesion("s", 1, "admin@logistica.local", "Admin", ["ADMINISTRADOR"], 5, ahora, ahora)
        id_inc = traz.registrar_incidente(e, "CLIMA", 4, "Prueba de integración", sesion)
        doc = cx.mongo["incidentes"].find_one({"id_envio": e})
        assert doc["contexto"]["ruta"] == "ARROS-NLRTM" and doc["pendiente_grafo"] is True
        assert any(p["id_incidente"] == id_inc for p in repo_incidentes.pendientes_grafo())

        # 3) ranking: la segunda vez sale de Redis; un incidente nuevo lo invalida
        filas, de_cache = traz.ranking_rutas()
        assert de_cache is False and any(f["ruta"] == "ARROS-NLRTM" for f in filas)
        assert traz.ranking_rutas()[1] is True
        traz.cambiar_estado_incidente(id_inc, "EN_ANALISIS", sesion)
        assert traz.ranking_rutas()[1] is False

        # 4) geoespacial: el evento de ARBUE está a menos de 50 km de Buenos Aires; el de Rosario no
        cerca = [x for x in traz.eventos_cerca("ARBUE", 50) if x.id_envio == e]
        assert {x.codigo_ubicacion for x in cerca} == {"ARBUE"}
    finally:
        cx.mongo["eventos_logisticos"].delete_many({"id_envio": e})
        cx.mongo["incidentes"].delete_many({"id_envio": e})
        cx.redis.delete(clave)
