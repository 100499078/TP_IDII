"""Fase 8: el plan de los datos de prueba respeta las reglas de la aplicación (sin bases de datos)."""
from collections import Counter
from datetime import datetime, timezone

import pytest

from app.dominio.modelos import MEDIO_DE_VEHICULO
from app.dominio.validaciones import validar_codigo_contenedor, validar_cuit
from herramientas.plan_datos import CLIENTES, OPERADORES, RUTAS, armar_plan, cuit, validar_estados

AHORA = datetime(2026, 10, 29, 15, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def plan():
    return armar_plan(AHORA)


def _estado_final(plan, e):
    estado = None
    for ev in e.eventos:
        if ev.momento > plan.ahora:
            continue
        if ev.tipo == "CREAR":
            estado = estado or "CREADO"
        elif ev.tipo == "CANCELAR":
            estado = "CANCELADO"
        elif ev.tipo == "ESTADO" and (estado != "CANCELADO"):
            estado = ev.datos["estado"]
    return estado


def test_cada_ruta_es_continua_y_respeta_las_fechas(plan):
    for e in plan.envios:
        assert e.tramos[0].origen == e.origen and e.tramos[-1].destino == e.destino
        for anterior, siguiente in zip(e.tramos, e.tramos[1:]):
            assert siguiente.origen == anterior.destino                      # ruta continua
            assert siguiente.salida >= anterior.llegada_estimada             # sale después de la llegada estimada
        assert e.tramos[0].salida > e.creacion and e.entrega_estimada > e.creacion


def test_operadores_y_vehiculos_hacen_ese_medio(plan):
    for e in plan.envios:
        for t in e.tramos:
            _, _, _, _, medios, vehiculos = OPERADORES[t.operador]
            assert t.medio in medios
            assert MEDIO_DE_VEHICULO[dict((i, tipo) for tipo, i in vehiculos)[t.vehiculo]] == t.medio


def test_los_estados_siguen_la_maquina_de_estados(plan):
    for e in plan.envios:
        cambios = sorted((ev.momento, ev.datos["estado"]) for ev in e.eventos if ev.tipo == "ESTADO")
        validar_estados(cambios)                                             # falla si alguna transición no existe
        llegadas = [ev.momento for ev in e.eventos if ev.tipo == "LLEGADA"]
        entregado = [m for m, s in cambios if s == "ENTREGADO"]
        if entregado:
            assert max(llegadas) <= entregado[0]                             # primero llega el último tramo


def test_validar_estados_detecta_transiciones_invalidas():
    with pytest.raises(ValueError):
        validar_estados([(AHORA, "ENTREGADO")])                              # CREADO -> ENTREGADO no existe


def test_un_contenedor_no_viaja_en_dos_envios_a_la_vez(plan):
    ocupado = {}
    for e in sorted(plan.envios, key=lambda x: x.creacion):
        for i in e.contenedores:
            assert ocupado.get(i, e.creacion) <= e.creacion, plan.contenedores[i].codigo
            ocupado[i] = e.liberacion or datetime.max.replace(tzinfo=timezone.utc)
        assert sum(plan.contenedores[i].capacidad for i in e.contenedores) >= e.volumen_m3
        assert {plan.contenedores[i].clase for i in e.contenedores} == {CLIENTES[e.cliente][6]}


def test_incidentes_mientras_viaja(plan):
    for e in plan.envios:
        salida = e.tramos[0].salida
        fin = max([ev.momento for ev in e.eventos if ev.tipo == "ESTADO"], default=salida)
        for ev in e.eventos:
            if ev.tipo == "INCIDENTE":
                assert salida < ev.momento < fin


def test_hoy_hay_envios_en_todos_los_estados(plan):
    finales = Counter(_estado_final(plan, e) for e in plan.envios)
    assert set(finales) == {"CREADO", "EN_TRANSITO", "EN_ADUANA", "DEMORADO", "ENTREGADO", "CANCELADO"}
    assert 70 <= len(plan.envios) <= 100


def test_es_reproducible_y_ningun_evento_es_futuro_para_el_ejecutor(plan):
    otro = armar_plan(AHORA)
    assert [(e.creacion, e.ruta, e.cliente) for e in otro.envios] == [(e.creacion, e.ruta, e.cliente)
                                                                      for e in plan.envios]
    assert all(ev.momento <= AHORA for ev in plan.eventos_hasta(AHORA))
    momentos = [ev.momento for ev in plan.eventos]
    assert momentos == sorted(momentos)


def test_lecturas_cerca_del_objetivo(plan):
    total = sum(len(ev.datos["momentos"]) * sum(len(plan.contenedores[i].sensores)
                                                for i in plan.envios[ev.datos["envio"] - 1].contenedores)
                for ev in plan.eventos_hasta(AHORA) if ev.tipo == "LECTURAS")
    assert 5_000 <= total <= 12_000


def test_cada_mes_se_cierra_y_despues_se_factura(plan):
    cierres = [ev for ev in plan.eventos if ev.tipo == "CIERRE"]
    facturas = [ev for ev in plan.eventos if ev.tipo == "FACTURAR"]
    assert len(cierres) == len(facturas) == 5                  # junio a octubre (empieza el 2 de mayo)
    for c, f in zip(cierres, facturas):
        assert c.momento.day == 1 and c.momento < f.momento
        assert c.datos["periodo"] == c.datos["desde"].strftime("%Y-%m")


def test_cuits_y_codigos_de_contenedor_validos(plan):
    cuits = [cuit(v[1]) for v in CLIENTES.values()] + [cuit(v[1]) for v in OPERADORES.values()]
    assert all(validar_cuit(c) is None for c in cuits) and len(set(cuits)) == len(cuits)
    assert all(validar_codigo_contenedor(k.codigo) is None for k in plan.contenedores)
    assert all(r[0] for r in RUTAS.values())
