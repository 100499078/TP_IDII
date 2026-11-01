"""Trazabilidad (fase 4): línea de tiempo desde el stream, incidentes, caché y tolerancia a fallos."""
from datetime import timedelta

import pytest

from app.dominio.modelos import Actor, Sesion
from app.excepciones import ErrorConexion, ErrorConflicto, ErrorValidacion
from app.servicios.envios import ServicioEnvios
from app.servicios.sincronizacion import Proyeccion, ServicioSincronizacion
from app.servicios.trazabilidad import ProyeccionTrazabilidad, ServicioTrazabilidad
from tests.fakes import (AHORA, ActoresEnMemoria, CacheEnMemoria, CatalogosEnMemoria, ColaEnMemoria,
                         ContenedoresEnMemoria, EnviosEnMemoria, EventosEnMemoria, GrafoEnMemoria,
                         IncidentesEnMemoria, OutboxEnMemoria)

SESION = Sesion("s1", 7, "op@x.com", "Olga", ["OPERADOR"], 3, AHORA, AHORA)


@pytest.fixture
def mundo():
    """Un envío ARROS → NLRTM con dos tramos: el primero ya llegó a ARBUE."""
    actores, catalogos, contenedores = ActoresEnMemoria(), CatalogosEnMemoria(), ContenedoresEnMemoria()
    envios = EnviosEnMemoria(contenedores)
    cliente = actores.crear(Actor(None, "CLIENTE", "Cliente SA", "30-71111111-1", "c@c.com", "AR"))
    operador = actores.crear(Actor(None, "OPERADOR", "Op SA", "30-71333333-2", "o@o.com", "AR",
                                   medios=["TERRESTRE", "MARITIMO"]))
    srv = ServicioEnvios(envios, actores, catalogos, contenedores, reloj=lambda: AHORA)
    e = srv.crear_envio(cliente, "ARROS", "NLRTM", AHORA + timedelta(days=30), 1000, 10, [1])
    t1 = srv.agregar_tramo(e, "TERRESTRE", "ARBUE", operador, AHORA + timedelta(hours=1), AHORA + timedelta(hours=5))
    t2 = srv.agregar_tramo(e, "MARITIMO", "NLRTM", operador, AHORA + timedelta(hours=6), AHORA + timedelta(days=20))
    envios.registrar_llegada(envios.tramo(t1), AHORA + timedelta(hours=6))
    srv.cambiar_estado(e, "EN_TRANSITO")
    eventos, incidentes = EventosEnMemoria(), IncidentesEnMemoria()
    traz = ServicioTrazabilidad(envios, catalogos, eventos, incidentes, CacheEnMemoria(),
                                reloj=lambda: AHORA + timedelta(hours=8))
    return dict(envios=envios, catalogos=catalogos, eventos=eventos, incidentes=incidentes, traz=traz,
                e=e, t1=t1, t2=t2)


def test_la_proyeccion_arma_la_linea_de_tiempo(mundo):
    p = ProyeccionTrazabilidad(mundo["eventos"], mundo["envios"], mundo["catalogos"])
    e = mundo["e"]
    p.aplicar_evento("ENVIO_CREADO", {"id_envio": e, "fecha_creacion": AHORA.isoformat(),
                                      "contenedores": [{"id_contenedor": 1}]}, "11")
    p.aplicar_evento("TRAMO_AGREGADO", {"id_tramo": mundo["t1"]}, "12")                 # no es un hito
    p.aplicar_evento("TRAMO_LLEGADA", {"id_tramo": mundo["t1"], "demora_h": 1.0,
                                       "fecha": (AHORA + timedelta(hours=6)).isoformat()}, "13")
    p.aplicar_evento("ENVIO_ESTADO", {"id_envio": e, "estado": "DEMORADO",
                                      "fecha": (AHORA + timedelta(hours=7)).isoformat()}, "14")
    lista = mundo["eventos"].de_envio(e)
    assert [(x.tipo, x.codigo_ubicacion) for x in lista] == [
        ("CARGA_INICIADA", "ARROS"), ("DESCARGA", "ARBUE"), ("RETRASO", "ARBUE")]   # el retraso, donde estaba


def test_un_evento_repetido_no_se_duplica(mundo):
    p = ProyeccionTrazabilidad(mundo["eventos"], mundo["envios"], mundo["catalogos"])
    payload = {"id_envio": mundo["e"], "fecha_creacion": AHORA.isoformat(), "contenedores": []}
    p.aplicar_evento("ENVIO_CREADO", payload, "21")
    p.aplicar_evento("ENVIO_CREADO", payload, "21")
    assert len(mundo["eventos"].eventos) == 1


def test_incidente_con_contexto_y_tramo_en_viaje(mundo):
    id_inc = mundo["traz"].registrar_incidente(mundo["e"], "CLIMA", 4, "Temporal en el puerto", SESION)
    i = mundo["incidentes"].obtener(id_inc)
    assert (i.ruta, i.pais_origen, i.pais_destino) == ("ARROS-NLRTM", "AR", "NL")
    assert (i.id_tramo, i.medio) == (mundo["t2"], "MARITIMO")      # a las 8 h viaja el tramo 2
    assert i.historial[0]["id_usuario"] == 7


def test_incidente_invalido(mundo):
    with pytest.raises(ErrorValidacion):
        mundo["traz"].registrar_incidente(mundo["e"], "CLIMA", 9, "x", SESION)
    with pytest.raises(ErrorValidacion, match="no es de este envío"):
        mundo["traz"].registrar_incidente(mundo["e"], "CLIMA", 3, "x", SESION, id_tramo=999)


def test_maquina_de_estados_del_incidente(mundo):
    traz = mundo["traz"]
    i = traz.registrar_incidente(mundo["e"], "ROBO", 5, "Faltan cajas", SESION)
    with pytest.raises(ErrorConflicto):
        traz.cambiar_estado_incidente(i, "CERRADO", SESION)           # primero hay que resolverlo
    for estado in ("EN_ANALISIS", "RESUELTO", "CERRADO"):
        traz.cambiar_estado_incidente(i, estado, SESION)
    assert [h["estado"] for h in mundo["incidentes"].obtener(i).historial] == [
        "ABIERTO", "EN_ANALISIS", "RESUELTO", "CERRADO"]
    with pytest.raises(ErrorConflicto, match="estado final"):
        traz.cambiar_estado_incidente(i, "EN_ANALISIS", SESION)


def test_ranking_cache_aside_e_invalidacion(mundo):
    traz, inc = mundo["traz"], mundo["incidentes"]
    traz.registrar_incidente(mundo["e"], "CLIMA", 2, "x", SESION)
    assert traz.ranking_rutas()[1] is False            # primera vez: se calcula en MongoDB
    assert traz.ranking_rutas()[1] is True             # segunda: viene de Redis
    assert inc.agregaciones == 1
    traz.registrar_incidente(mundo["e"], "ROBO", 3, "y", SESION)    # invalida la caché
    filas, de_cache = traz.ranking_rutas()
    assert de_cache is False and filas[0]["incidentes"] == 2


def test_seguimiento_sin_mongodb_muestra_lo_demas(mundo):
    mundo["eventos"].caido = True
    s = mundo["traz"].seguimiento(mundo["e"])
    assert s["envio"].id_envio == mundo["e"] and s["no_disponible"] == ["MongoDB (eventos e incidentes)"]


def test_dos_proyecciones_independientes_y_outbox_de_incidentes(mundo):
    """Neo4j caído: MongoDB igual recibe los eventos; el incidente viaja al stream y a Neo4j después."""
    outbox = OutboxEnMemoria([{"id_evento": 1, "tipo_evento": "ENVIO_CREADO",
                               "payload": {"id_envio": mundo["e"], "fecha_creacion": AHORA.isoformat(),
                                           "contenedores": []}}])
    cola = ColaEnMemoria()
    grafo = GrafoEnMemoria(fallar_veces=1)
    # Las dos proyecciones leen el MISMO stream, cada una con su grupo (su propia posición)
    cola_traz = ColaEnMemoria()
    cola_traz.mensajes = cola.mensajes
    traz = Proyeccion("MongoDB", cola_traz,
                      ProyeccionTrazabilidad(mundo["eventos"], mundo["envios"], mundo["catalogos"]))
    sync = ServicioSincronizacion(outbox, cola, grafo, otras=[traz], repo_incidentes=mundo["incidentes"])
    mundo["traz"].registrar_incidente(mundo["e"], "CLIMA", 3, "x", SESION)
    with pytest.raises(ConnectionError):
        sync.ciclo()                                   # Neo4j caído...
    assert [x.tipo for x in mundo["eventos"].eventos] == ["CARGA_INICIADA"]   # ...pero MongoDB al día
    assert mundo["incidentes"].pendientes == set()     # el incidente ya está a salvo en el stream
    sync.ciclo()                                       # Neo4j volvió
    assert [t for t, _ in grafo.aplicados] == ["ENVIO_CREADO", "INCIDENTE_REGISTRADO"]
    assert len(mundo["eventos"].eventos) == 1          # MongoDB no lo aplicó dos veces


def test_sin_mongodb_el_grafo_sigue(mundo):
    outbox = OutboxEnMemoria([{"id_evento": 1, "tipo_evento": "ENVIO_ESTADO",
                               "payload": {"id_envio": mundo["e"], "estado": "EN_TRANSITO",
                                           "fecha": AHORA.isoformat(), "contenedores_liberados": []}}])
    cola = ColaEnMemoria()
    cola_traz = ColaEnMemoria()
    cola_traz.mensajes = cola.mensajes
    mundo["eventos"].caido = True
    grafo = GrafoEnMemoria()
    traz = Proyeccion("MongoDB", cola_traz,
                      ProyeccionTrazabilidad(mundo["eventos"], mundo["envios"], mundo["catalogos"]))
    sync = ServicioSincronizacion(outbox, cola, grafo, otras=[traz])
    with pytest.raises(ErrorConexion):
        sync.ciclo()
    assert len(grafo.aplicados) == 1
