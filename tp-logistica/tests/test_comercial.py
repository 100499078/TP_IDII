"""Gestión comercial (fase 6): reglas del servicio con dobles en memoria."""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.dominio.modelos import Actor, Factura, Sesion, pesos
from app.excepciones import EntidadNoEncontrada, ErrorConflicto, ErrorValidacion
from app.servicios.comercial import ServicioComercial
from tests.fakes import AHORA, ActoresEnMemoria


class ComercialEnMemoria:
    def __init__(self):
        self.facturables = {}            # id_cliente -> [envíos]
        self.emitidas = []
        self.cuentas = {}                # id_actor -> {"saldo", "limite_credito", "fecha_actualizacion"}
        self.pagos = []
        self.falla_con = set()           # clientes cuya factura choca con el índice único

    def envios_facturables(self, id_cliente):
        return self.facturables.get(id_cliente, [])

    def clientes_con_envios_facturables(self):
        return sorted(k for k, v in self.facturables.items() if v)

    def emitir(self, id_cliente, items, fecha, vencimiento):
        if id_cliente in self.falla_con:
            raise ErrorConflicto("Alguno de los envíos ya fue facturado")
        total = pesos(sum((i.subtotal for i in items), Decimal(0)))
        f = Factura(id_cliente, "EMITIDA", f"A-{len(self.emitidas) + 1:08d}", fecha, vencimiento, total, items=items,
                    id_factura=len(self.emitidas) + 1)
        self.emitidas.append(f)
        self.facturables[id_cliente] = []
        return f

    def registrar_pago(self, id_factura, monto, medio, fecha):
        self.pagos.append((id_factura, monto, medio))
        return "PARCIAL"

    def registrar_recibida(self, id_actor, numero, fecha, vencimiento, importe, descripcion):
        return 99

    def cuenta(self, id_actor):
        return self.cuentas.get(id_actor)


class LocksEnMemoria:
    def __init__(self, ocupado=False):
        self.ocupado, self.soltados = ocupado, 0

    def tomar(self, nombre, segundos):
        return None if self.ocupado else "token"

    def soltar(self, nombre, token):
        self.soltados += 1
        return True


@pytest.fixture
def com():
    actores = ActoresEnMemoria()
    c1 = actores.crear(Actor(None, "CLIENTE", "Cliente Uno SA", "30-71111111-1", "a@a.com", "AR",
                             condicion_pago_dias=30))
    c2 = actores.crear(Actor(None, "CLIENTE", "Cliente Dos SA", "30-71222222-7", "b@b.com", "AR",
                             condicion_pago_dias=60))
    op = actores.crear(Actor(None, "OPERADOR", "Op SA", "30-71333333-2", "o@o.com", "AR", medios=["TERRESTRE"]))
    repo, locks = ComercialEnMemoria(), LocksEnMemoria()
    repo.facturables[c1] = [{"id_envio": 7, "origen": "ARROS", "destino": "ARBUE", "costo": Decimal("27000")},
                            {"id_envio": 8, "origen": "ARBUE", "destino": "UYMVD", "costo": Decimal("1000.10")}]
    srv = ServicioComercial(repo, actores, locks, reloj=lambda: AHORA)
    return dict(srv=srv, repo=repo, locks=locks, c1=c1, c2=c2, op=op)


def test_factura_con_un_item_por_envio_mas_iva_y_vencimiento(com):
    f = com["srv"].facturar_cliente(com["c1"])
    assert [i.id_envio for i in f.items] == [7, 8, None]
    assert f.items[-1].precio_unitario == Decimal("5880.02")    # 21 % de 28000,10 = 5880,021, redondeado
    assert f.importe_total == pesos("28000.10") + pesos("5880.02")
    assert f.fecha_vencimiento == AHORA.date() + timedelta(days=30)
    assert com["srv"].facturar_cliente(com["c1"]) is None                       # ya no queda nada


def test_facturacion_masiva_suelta_el_lock_y_sigue_si_un_cliente_falla(com):
    com["repo"].facturables[com["c2"]] = [{"id_envio": 9, "origen": "A", "destino": "B", "costo": Decimal(10)}]
    com["repo"].falla_con.add(com["c1"])
    emitidas = com["srv"].facturar_todos()
    assert [f.id_actor for f in emitidas] == [com["c2"]]
    assert com["locks"].soltados == 1


def test_facturacion_masiva_con_el_lock_tomado(com):
    com["locks"].ocupado = True
    with pytest.raises(ErrorConflicto, match="Otra persona"):
        com["srv"].facturar_todos()
    assert com["repo"].emitidas == []


@pytest.mark.parametrize("monto,medio", [("0", "EFECTIVO"), ("-5", "EFECTIVO"), ("abc", "EFECTIVO"),
                                         ("10", "BITCOIN")])
def test_pagos_invalidos(com, monto, medio):
    with pytest.raises(ErrorValidacion):
        com["srv"].registrar_pago(1, monto, medio)


def test_pago_acepta_coma_decimal(com):
    com["srv"].registrar_pago(1, "1500,50", "CHEQUE")
    assert com["repo"].pagos == [(1, Decimal("1500.50"), "CHEQUE")]


def test_factura_recibida_solo_de_operadores_o_proveedores(com):
    assert com["srv"].registrar_factura_recibida(com["op"], "b-0001-1", "100", 30, "Fletes") == 99
    with pytest.raises(EntidadNoEncontrada):
        com["srv"].registrar_factura_recibida(com["c1"], "B-0001-2", "100", 30, "x")
    with pytest.raises(ErrorValidacion):
        com["srv"].registrar_factura_recibida(com["op"], "B-0001-3", "0", 30, "x")


def test_control_de_credito(com):
    srv, repo = com["srv"], com["repo"]
    repo.cuentas[com["c1"]] = {"saldo": Decimal("600"), "limite_credito": Decimal("500"), "fecha_actualizacion": None}
    with pytest.raises(ErrorConflicto, match="límite de crédito"):
        srv.controlar_credito(com["c1"])
    repo.cuentas[com["c2"]] = {"saldo": Decimal("9999"), "limite_credito": Decimal("0"), "fecha_actualizacion": None}
    srv.controlar_credito(com["c2"])                    # límite 0 = sin límite configurado


def test_un_cliente_solo_ve_lo_suyo():
    cliente = Sesion("s", 3, "c@c.com", "C", ["CLIENTE"], 1, AHORA, AHORA, id_actor=10)
    interno = Sesion("s", 1, "a@a.com", "A", ["ADMINISTRADOR"], 5, AHORA, AHORA)
    assert ServicioComercial.actor_visible(cliente, 99) == 10
    assert ServicioComercial.actor_visible(interno, 99) == 99


def test_crear_envio_respeta_el_credito():
    """La regla de crédito llega a la fase 2: ServicioEnvios recibe el control como dependencia."""
    from tests.test_envios import entorno  # noqa: F401
    from app.servicios.envios import ServicioEnvios
    from tests.fakes import CatalogosEnMemoria, ContenedoresEnMemoria, EnviosEnMemoria
    actores, contenedores = ActoresEnMemoria(), ContenedoresEnMemoria()
    c = actores.crear(Actor(None, "CLIENTE", "Deudor SA", "30-71111111-1", "d@d.com", "AR"))

    def control(id_cliente):
        raise ErrorConflicto("El cliente debe $ 600 y su límite de crédito es $ 500")
    srv = ServicioEnvios(EnviosEnMemoria(contenedores), actores, CatalogosEnMemoria(), contenedores,
                         reloj=lambda: AHORA, control_credito=control)
    with pytest.raises(ErrorConflicto, match="límite"):
        srv.crear_envio(c, "ARROS", "NLRTM", AHORA + timedelta(days=5), 100, 1, [1])
    assert contenedores.contenedor(1).estado == "DISPONIBLE"      # no se tocó nada
