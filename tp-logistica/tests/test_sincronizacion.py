"""Sincronización outbox → stream → grafo, con dobles en memoria."""
import pytest

from app.repositorios.neo4j.grafo import SENTENCIAS, RepositorioGrafo
from app.servicios.sincronizacion import MAX_ENTREGAS, ServicioSincronizacion
from tests.fakes import ColaEnMemoria, GrafoEnMemoria, OutboxEnMemoria


def eventos(n):
    return [{"id_evento": i, "tipo_evento": "ENVIO_ESTADO",
             "payload": {"id_envio": i, "estado": "EN_TRANSITO", "fecha": "2026-11-01T12:00:00+00:00",
                         "contenedores_liberados": []}} for i in range(1, n + 1)]


def armar(grafo, n=3):
    outbox, cola = OutboxEnMemoria(eventos(n)), ColaEnMemoria()
    return ServicioSincronizacion(outbox, cola, grafo), outbox, cola


def test_ciclo_completo_en_orden():
    grafo = GrafoEnMemoria()
    srv, outbox, cola = armar(grafo)
    assert srv.ciclo() == (3, 3)
    assert [p["id_envio"] for _, p in grafo.aplicados] == [1, 2, 3]
    assert outbox.contar_pendientes() == 0 and cola.estado()["sin_confirmar"] == 0


def test_si_neo4j_se_cae_los_eventos_quedan_pendientes_y_se_reintentan():
    grafo = GrafoEnMemoria(fallar_veces=1)            # falla "de conexión" (transitoria)
    srv, outbox, cola = armar(grafo)
    with pytest.raises(ConnectionError):
        srv.ciclo()                                    # el hilo lo registra y espera para reintentar
    assert outbox.contar_pendientes() == 0             # ya están a salvo en Redis
    assert cola.estado()["sin_confirmar"] == 3
    assert srv.ciclo() == (0, 3)                       # el ciclo siguiente reprocesa los pendientes
    assert [p["id_envio"] for _, p in grafo.aplicados] == [1, 2, 3]


def test_una_caida_larga_de_neo4j_no_manda_eventos_a_fallidos():
    grafo = GrafoEnMemoria(fallar_veces=MAX_ENTREGAS * 2)
    srv, _, cola = armar(grafo, n=1)
    for _ in range(MAX_ENTREGAS * 2):
        with pytest.raises(ConnectionError):
            srv.ciclo()
    assert srv.ciclo() == (0, 1)
    assert cola.estado()["fallidos"] == 0


def test_evento_que_siempre_falla_va_a_fallidos():
    grafo = GrafoEnMemoria(fallar_siempre_con="ENVIO_ESTADO")
    srv, _, cola = armar(grafo, n=1)
    for _ in range(MAX_ENTREGAS):
        srv.ciclo()
    assert cola.estado() == {"en_stream": 1, "sin_confirmar": 0, "fallidos": 1}


def test_proveedores_no_van_al_grafo_y_tipos_desconocidos_fallan():
    assert RepositorioGrafo.sentencias_de("ACTOR_GUARDADO", {"tipo": "PROVEEDOR"}) == []
    assert RepositorioGrafo.sentencias_de("ACTOR_GUARDADO", {"tipo": "CLIENTE"})
    try:
        RepositorioGrafo.sentencias_de("INVENTADO", {})
        assert False, "debería fallar"
    except ValueError:
        pass


def test_toda_sentencia_cypher_usa_merge_para_ser_idempotente():
    for clave, sentencias in SENTENCIAS.items():
        texto = " ".join(sentencias)
        assert " CREATE " not in f" {texto} ".replace("ON CREATE", ""), clave
