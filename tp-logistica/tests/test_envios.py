"""Reglas de negocio de envíos y tramos, con repositorios en memoria."""
from datetime import timedelta

import pytest

from app.dominio.modelos import Actor, Vehiculo
from app.excepciones import EntidadNoEncontrada, ErrorConflicto, ErrorValidacion
from app.servicios.envios import ServicioEnvios
from tests.fakes import (AHORA, ActoresEnMemoria, CatalogosEnMemoria, ContenedoresEnMemoria,
                         EnviosEnMemoria)


@pytest.fixture
def entorno():
    actores, catalogos, contenedores = ActoresEnMemoria(), CatalogosEnMemoria(), ContenedoresEnMemoria()
    envios = EnviosEnMemoria(contenedores)
    cliente = actores.crear(Actor(None, "CLIENTE", "Cliente SA", "30-71111111-1", "c@c.com", "AR"))
    camionero = actores.crear(Actor(None, "OPERADOR", "Camiones SA", "30-71333333-2", "o@o.com", "AR",
                                    medios=["TERRESTRE"]))
    naviera = actores.crear(Actor(None, "OPERADOR", "Naviera SA", "30-71444444-8", "n@n.com", "UY",
                                  medios=["MARITIMO"]))
    buque = catalogos.crear_vehiculo(Vehiculo(None, "BUQUE", "IMO1", naviera))
    srv = ServicioEnvios(envios, actores, catalogos, contenedores, reloj=lambda: AHORA)
    return dict(srv=srv, cliente=cliente, camionero=camionero, naviera=naviera, buque=buque,
                contenedores=contenedores)


def crear(e, contenedores=(1,), volumen=20.0):
    return e["srv"].crear_envio(e["cliente"], "ARROS", "NLRTM", AHORA + timedelta(days=30), 1000, volumen,
                                list(contenedores))


def tramo(e, id_envio, medio, destino, operador, horas=0, vehiculo=None):
    salida = AHORA + timedelta(hours=1 + horas)
    return e["srv"].agregar_tramo(id_envio, medio, destino, operador, salida, salida + timedelta(hours=5), vehiculo)


def test_crear_envio_ocupa_los_contenedores(entorno):
    crear(entorno)
    assert entorno["contenedores"].contenedor(1).estado == "EN_USO"


def test_no_se_puede_usar_un_contenedor_ocupado(entorno):
    crear(entorno)
    with pytest.raises(ErrorConflicto):
        crear(entorno)


def test_volumen_mayor_a_la_capacidad(entorno):
    with pytest.raises(ErrorValidacion, match="capacidad"):
        crear(entorno, volumen=31)


def test_origen_inexistente(entorno):
    with pytest.raises(EntidadNoEncontrada):
        entorno["srv"].crear_envio(entorno["cliente"], "XXXXX", "NLRTM", AHORA + timedelta(days=1), 1, 1, [1])


def test_la_ruta_es_continua_y_el_costo_usa_la_tarifa(entorno):
    e = crear(entorno)
    tramo(entorno, e, "TERRESTRE", "ARBUE", entorno["camionero"])
    tramo(entorno, e, "MARITIMO", "NLRTM", entorno["naviera"], horas=10, vehiculo=entorno["buque"])
    envio = entorno["srv"].obtener(e)
    assert [(t.origen, t.destino) for t in envio.tramos] == [("ARROS", "ARBUE"), ("ARBUE", "NLRTM")]
    assert envio.tramos[0].costo == 1000 + 2 * 1000      # base + precio_kg × peso
    assert envio.costo_total == 3000 + 6000


def test_operador_sin_ese_medio(entorno):
    e = crear(entorno)
    with pytest.raises(ErrorValidacion, match="no trabaja"):
        tramo(entorno, e, "MARITIMO", "ARBUE", entorno["camionero"])


def test_vehiculo_de_otro_operador(entorno):
    e = crear(entorno)
    with pytest.raises(ErrorValidacion, match="no pertenece"):
        tramo(entorno, e, "TERRESTRE", "ARBUE", entorno["camionero"], vehiculo=entorno["buque"])


def test_tramo_que_sale_antes_de_que_llegue_el_anterior(entorno):
    e = crear(entorno)
    tramo(entorno, e, "TERRESTRE", "ARBUE", entorno["camionero"])
    with pytest.raises(ErrorValidacion, match="anterior"):
        tramo(entorno, e, "MARITIMO", "NLRTM", entorno["naviera"], horas=2)


def test_maquina_de_estados(entorno):
    e = crear(entorno)
    with pytest.raises(ErrorConflicto, match="al menos un tramo"):
        entorno["srv"].cambiar_estado(e, "EN_TRANSITO")
    with pytest.raises(ErrorConflicto, match="no puede pasar"):
        entorno["srv"].cambiar_estado(e, "ENTREGADO")       # CREADO → ENTREGADO no está permitido
    tramo(entorno, e, "TERRESTRE", "ARBUE", entorno["camionero"])
    entorno["srv"].cambiar_estado(e, "EN_TRANSITO")
    with pytest.raises(ErrorConflicto, match="termina en ARBUE"):
        entorno["srv"].cambiar_estado(e, "ENTREGADO")       # la ruta no llega al destino


def test_entregar_libera_los_contenedores(entorno):
    e = crear(entorno)
    tramo(entorno, e, "TERRESTRE", "ARBUE", entorno["camionero"])
    t2 = tramo(entorno, e, "MARITIMO", "NLRTM", entorno["naviera"], horas=10)
    entorno["srv"].cambiar_estado(e, "EN_TRANSITO")
    with pytest.raises(ErrorConflicto, match="llegada del último tramo"):
        entorno["srv"].cambiar_estado(e, "ENTREGADO")
    entorno["srv"].registrar_llegada(t2, AHORA + timedelta(hours=20))
    assert entorno["srv"].cambiar_estado(e, "ENTREGADO") == [1]
    assert entorno["contenedores"].contenedor(1).estado == "DISPONIBLE"
    with pytest.raises(ErrorConflicto):
        tramo(entorno, e, "TERRESTRE", "ARBUE", entorno["camionero"], horas=40)   # ya terminó


def test_un_cliente_no_ve_envios_ajenos(entorno):
    e = crear(entorno)
    assert entorno["srv"].obtener(e, id_cliente=entorno["cliente"]).id_envio == e
    with pytest.raises(EntidadNoEncontrada):
        entorno["srv"].obtener(e, id_cliente=999)
