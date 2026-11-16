"""Integración de la fase 6 contra PostgreSQL y Redis reales.

Factura un envío entregado, registra pagos (incluidos dos pagos simultáneos desde dos
conexiones), anula, revisa el saldo que mantiene el trigger y el lock de Redis.
Borra todo lo que crea.
"""
import threading
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.config import Config
from app.dominio.modelos import IVA, ItemFactura, pesos
from app.excepciones import ErrorConflicto
from app.infra.conexiones import GestorConexiones
from app.repositorios.postgres.actores import RepositorioActores
from app.repositorios.postgres.catalogos import RepositorioCatalogos
from app.repositorios.postgres.comercial import RepositorioComercial
from app.repositorios.postgres.contenedores import RepositorioContenedores
from app.repositorios.postgres.envios import RepositorioEnvios
from app.repositorios.redis.locks import RepositorioLocks
from app.servicios.comercial import ServicioComercial
from app.servicios.envios import ServicioEnvios
from tests.integracion.test_operativa import cx, escenario  # noqa: F401  (fixtures)

pytestmark = pytest.mark.integracion


def _envio_entregado(cx, escenario):  # noqa: F811
    srv = ServicioEnvios(RepositorioEnvios(cx), RepositorioActores(cx), RepositorioCatalogos(cx),
                         RepositorioContenedores(cx))
    ahora = datetime.now(timezone.utc).replace(microsecond=0)
    e = srv.crear_envio(escenario["cliente"], "ARROS", "ARBUE", ahora + timedelta(days=5), 1000, 10,
                        [escenario["contenedor"]])
    t = srv.agregar_tramo(e, "TERRESTRE", "ARBUE", escenario["operador"], ahora + timedelta(minutes=1),
                          ahora + timedelta(hours=8))
    srv.cambiar_estado(e, "EN_TRANSITO")
    srv.registrar_llegada(t, ahora + timedelta(minutes=2))
    srv.cambiar_estado(e, "ENTREGADO")
    return e


def _limpiar(cx, actores):  # noqa: F811
    with cx.transaccion() as cur:
        cur.execute("""DELETE FROM movimiento_cc WHERE id_cuenta IN
                       (SELECT id_cuenta FROM cuenta_corriente WHERE id_actor = ANY(%s))""", (actores,))
        cur.execute("DELETE FROM pago WHERE id_factura IN (SELECT id_factura FROM factura WHERE id_actor = ANY(%s))",
                    (actores,))
        cur.execute("DELETE FROM factura WHERE id_actor = ANY(%s)", (actores,))     # ítems en cascada


def test_factura_pagos_anulacion_y_saldo(cx, escenario):  # noqa: F811
    repo = RepositorioComercial(cx)
    srv = ServicioComercial(repo, RepositorioActores(cx), RepositorioLocks(cx))
    cliente, operador = escenario["cliente"], escenario["operador"]
    try:
        e = _envio_entregado(cx, escenario)
        facturables = srv.envios_facturables(cliente)
        assert [f["id_envio"] for f in facturables] == [e]
        neto = pesos(facturables[0]["costo"])
        assert neto == pesos(15000 + 12 * 1000)                     # tarifa terrestre de los datos iniciales

        f = srv.facturar_cliente(cliente)
        assert f.importe_total == pesos(neto + neto * IVA)
        assert repo.cuenta(cliente)["saldo"] == f.importe_total      # el trigger sumó el débito
        assert srv.facturar_cliente(cliente) is None                 # ya no queda nada para facturar
        with pytest.raises(ErrorConflicto, match="ya fue facturado"):  # índice único ux_item_factura_envio
            repo.emitir(cliente, [ItemFactura("otra vez", Decimal(1), Decimal(10), id_envio=e)], date.today(),
                        date.today())

        assert srv.registrar_pago(f.id_factura, "1000", "TRANSFERENCIA") == "PARCIAL"
        with pytest.raises(ErrorConflicto, match="supera"):
            srv.registrar_pago(f.id_factura, str(f.importe_total), "TARJETA")
        assert srv.registrar_pago(f.id_factura, str(f.importe_total - 1000), "TARJETA") == "PAGADA"
        assert repo.cuenta(cliente)["saldo"] == 0
        with pytest.raises(ErrorConflicto):
            srv.anular(f.id_factura)                                 # tiene pagos
        movimientos = repo.movimientos(cliente)
        assert [m["tipo"] for m in movimientos] == ["DEBITO", "CREDITO", "CREDITO"]
        assert movimientos[-1]["saldo"] == 0                         # el acumulado coincide con el saldo

        # Factura de un operador: CRÉDITO (le debemos); al anularla, el contraasiento la compensa
        id_rec = srv.registrar_factura_recibida(operador, "B-0001-00000001", "5000,50", 30, "Fletes")
        assert repo.cuenta(operador)["saldo"] == pesos("-5000.50")
        srv.anular(id_rec)
        assert repo.cuenta(operador)["saldo"] == 0
    finally:
        _limpiar(cx, [cliente, operador])


def test_dos_pagos_simultaneos_no_pagan_de_mas(cx, escenario):  # noqa: F811
    """Dos conexiones pagan el total de la misma factura a la vez: FOR UPDATE hace que una espere."""
    cliente = escenario["cliente"]
    repo = RepositorioComercial(cx)
    try:
        f = repo.emitir(cliente, [ItemFactura("Servicio", Decimal(1), Decimal("1000.00"))], date.today(),
                        date.today() + timedelta(days=30))
        resultados = []

        def pagar():
            propia = GestorConexiones(Config.desde_entorno())
            try:
                resultados.append(RepositorioComercial(propia).registrar_pago(
                    f.id_factura, Decimal("1000.00"), "EFECTIVO", datetime.now(timezone.utc)))
            except ErrorConflicto as exc:
                resultados.append(type(exc).__name__)
            finally:
                propia.cerrar()

        hilos = [threading.Thread(target=pagar) for _ in range(2)]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join()
        assert sorted(resultados) == ["ErrorConflicto", "PAGADA"]    # uno pagó; el otro vio que ya no faltaba nada
        assert repo.cuenta(cliente)["saldo"] == 0
    finally:
        _limpiar(cx, [cliente])


def test_vencidas_antiguedad_y_credito(cx, escenario):  # noqa: F811
    cliente = escenario["cliente"]
    repo = RepositorioComercial(cx)
    srv = ServicioComercial(repo, RepositorioActores(cx))
    hace_40 = date.today() - timedelta(days=40)
    try:
        repo.emitir(cliente, [ItemFactura("Vieja", Decimal(1), Decimal("700.00"))], hace_40, hace_40 + timedelta(days=5))
        assert srv.marcar_vencidas() >= 1
        fila = next(f for f in srv.antiguedad_deuda() if f["id_actor"] == cliente)
        assert fila["d31_60"] == Decimal("700.00") and fila["total"] == Decimal("700.00")

        with cx.transaccion() as cur:
            cur.execute("UPDATE cliente SET limite_credito = 500 WHERE id_actor = %s", (cliente,))
        with pytest.raises(ErrorConflicto, match="límite de crédito"):
            srv.controlar_credito(cliente)                           # debe 700 y su límite es 500
    finally:
        _limpiar(cx, [cliente])


def test_lock_de_redis(cx):  # noqa: F811
    locks = RepositorioLocks(cx)
    token = locks.tomar("prueba-integracion", 30)
    try:
        assert token and locks.tomar("prueba-integracion", 30) is None   # el segundo no lo consigue
        assert locks.soltar("prueba-integracion", "token-ajeno") is False  # solo lo suelta su dueño
        assert 0 < locks.segundos_restantes("prueba-integracion") <= 30
    finally:
        assert locks.soltar("prueba-integracion", token) is True
