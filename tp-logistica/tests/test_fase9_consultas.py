"""Fase 9 (segunda parte): reportes facturables, reporte predictivo, rol SOCIO, mapa y control de reportes cobrados."""
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.dominio.modelos import PRECIO_REPORTE, Actor, Envio
from app.excepciones import EntidadNoEncontrada, ErrorValidacion
from app.servicios.comercial import ServicioComercial
from app.servicios.mapas import html_mapa
from app.servicios.reportes import ServicioReportes
from app.servicios.trazabilidad import ServicioTrazabilidad
from app.servicios.usuarios import ServicioUsuarios
from app.servicios.verificacion import ADVERTENCIA, ERROR, OK, Control, ServicioVerificacion
from tests.fakes import AHORA, ActoresEnMemoria
from tests.test_comercial import ComercialEnMemoria, LocksEnMemoria
from tests.test_envios import crear, entorno  # noqa: F401  (fixture)
from tests.test_procesos import ColaReportesEnMemoria, Grafo, Incidentes, SQLFalso


# ---------------------------------------------------------------- reportes facturables
class ReportesMongo:
    """Lo que la facturación usa de la colección `reportes`."""

    def __init__(self, docs):
        self.docs = {d["id_reporte"]: d for d in docs}

    def facturables(self, id_cliente=None):
        return [dict(d) for d in self.docs.values() if d["estado"] == "GENERADO" and "id_factura" not in d
                and id_cliente in (None, d["id_cliente"])]

    def clientes_con_facturables(self):
        return sorted({d["id_cliente"] for d in self.facturables()})

    def marcar_facturados(self, ids, id_factura, numero):
        for i in ids:
            self.docs[i].update(id_factura=id_factura, numero_factura=numero)

    def desmarcar_facturados(self, ids):
        for i in ids:
            self.docs[i].pop("id_factura", None)


class ComercialConReportes(ComercialEnMemoria):
    def __init__(self):
        super().__init__()
        self.en_facturas = {}                    # id_reporte -> {id_factura, numero}

    def emitir(self, id_cliente, items, fecha, vencimiento):
        f = super().emitir(id_cliente, items, fecha, vencimiento)
        for i in items:
            if i.id_reporte:
                self.en_facturas[i.id_reporte] = {"id_factura": f.id_factura, "numero": f.numero}
        return f

    def reportes_ya_facturados(self, ids):
        return {i: self.en_facturas[i] for i in ids if i in self.en_facturas}

    def anular(self, id_factura):
        libres = [i for i, f in self.en_facturas.items() if f["id_factura"] == id_factura]
        for i in libres:
            del self.en_facturas[i]
        return libres


def reporte(i, cliente, tipo="ENVIOS_DEMORADOS"):
    return {"id_reporte": f"{i:024x}", "id_cliente": cliente, "tipo": tipo, "estado": "GENERADO",
            "facturable": True, "fecha_solicitud": AHORA - timedelta(days=2)}


@pytest.fixture
def fac():
    actores = ActoresEnMemoria()
    c1 = actores.crear(Actor(None, "CLIENTE", "Cliente Uno SA", "30-71111111-1", "a@a.com", "AR",
                             condicion_pago_dias=30))
    repo, mongo = ComercialConReportes(), ReportesMongo([reporte(1, 1), reporte(2, 1, "RIESGO_DEMORA")])
    srv = ServicioComercial(repo, actores, LocksEnMemoria(), reloj=lambda: AHORA, repo_reportes=mongo)
    return srv, repo, mongo, c1


def test_los_reportes_del_cliente_se_cobran_en_su_factura(fac):
    srv, repo, mongo, c1 = fac
    f = srv.facturar_cliente(c1)                                            # sin envíos: solo los 2 reportes
    assert [i.id_reporte for i in f.items] == [f"{1:024x}", f"{2:024x}", None]          # + IVA
    assert f.importe_total == PRECIO_REPORTE * 2 * Decimal("1.21")
    assert all(d["numero_factura"] == f.numero for d in mongo.docs.values())           # marcados en MongoDB
    assert srv.facturar_cliente(c1) is None                                 # no se cobran dos veces


def test_si_la_marca_en_mongo_se_perdio_no_se_cobra_de_nuevo(fac):
    srv, repo, mongo, c1 = fac
    repo.en_facturas[f"{1:024x}"] = {"id_factura": 9, "numero": "A-00000009"}          # ya facturado en PostgreSQL
    pendientes = srv.reportes_facturables(c1)
    assert [r["id_reporte"] for r in pendientes] == [f"{2:024x}"]
    assert mongo.docs[f"{1:024x}"]["numero_factura"] == "A-00000009"     # y se reconcilia la marca


def test_facturacion_masiva_incluye_clientes_con_solo_reportes_y_anular_los_libera(fac):
    srv, repo, mongo, c1 = fac
    emitidas = srv.facturar_todos()
    assert [f.id_actor for f in emitidas] == [c1]
    srv.anular(emitidas[0].id_factura)
    assert len(srv.reportes_facturables(c1)) == 2                           # se pueden volver a cobrar


# ---------------------------------------------------------------- reportes de un cliente
@pytest.fixture
def rep(tmp_path):
    cola = ColaReportesEnMemoria()
    cola.solicitar = (lambda f: lambda *a, id_cliente=None: (f(*a), cola.docs[max(cola.docs)].update(
        id_cliente=id_cliente, facturable=id_cliente is not None))[0])(cola.solicitar)
    pred = SimpleNamespace(actuales=lambda n: [
        {"id_envio": 5, "nivel": "ALTO", "probabilidad_demora": 0.6, "riesgo_operativo": 0.5, "riesgo_climatico": 0.3,
         "fecha_calculo": AHORA, "modelo": {"nombre": "h", "version": "1.0"}, "variables": {}},
        {"id_envio": 6, "nivel": "BAJO", "probabilidad_demora": 0.1, "riesgo_operativo": 0.0, "riesgo_climatico": 0.3,
         "fecha_calculo": AHORA, "modelo": {"nombre": "h", "version": "1.0"}, "variables": {}}])
    sql = SQLFalso()
    sql.envios_para_incidentes = lambda desde, hasta, id_cliente=None: [{"id_envio": 5}]   # los del cliente
    return ServicioReportes(cola, sql, Incidentes(), Grafo(), tmp_path, reloj=lambda: AHORA,
                            repo_predicciones=pred), cola


def test_reporte_para_un_cliente_es_facturable_y_filtra_sus_datos(rep):
    srv, cola = rep
    i = srv.solicitar(7, "RIESGO_DEMORA", id_cliente=3)
    assert cola.docs[i]["facturable"] is True and cola.docs[i]["id_cliente"] == 3
    srv.procesar_pendientes()
    assert [f["id_envio"] for f in cola.docs[i]["resultado"]] == [5]      # solo el envío del cliente
    interno = srv.solicitar(7, "RIESGO_DEMORA")
    srv.procesar_pendientes()
    assert len(cola.docs[interno]["resultado"]) == 2 and not cola.docs[interno]["facturable"]


def test_reportes_internos_no_se_piden_para_un_cliente(rep):
    srv, _ = rep
    with pytest.raises(ErrorValidacion, match="interno"):
        srv.solicitar(7, "RANKING_RUTAS", id_cliente=3)


# ---------------------------------------------------------------- rol SOCIO y envíos de un operador
def test_un_socio_pertenece_a_un_operador_o_proveedor():
    actores = ActoresEnMemoria()
    cliente = actores.crear(Actor(None, "CLIENTE", "C SA", "30-71111111-1", "c@c.com", "AR"))
    operador = actores.crear(Actor(None, "OPERADOR", "O SA", "30-71333333-2", "o@o.com", "AR", medios=["TERRESTRE"]))
    repo = SimpleNamespace(listar_roles=lambda: [SimpleNamespace(nombre=n) for n in ("OPERADOR", "CLIENTE", "SOCIO")])
    srv = ServicioUsuarios(repo, actores, None)
    srv._validar_roles(["SOCIO"], operador)                                 # bien
    for roles, actor in ((["SOCIO"], cliente), (["SOCIO"], None), (["OPERADOR"], operador)):
        with pytest.raises(ErrorValidacion):
            srv._validar_roles(roles, actor)


def test_un_operador_solo_ve_los_envios_que_mueve(entorno):  # noqa: F811
    srv = entorno["srv"]
    e = crear(entorno)
    srv.agregar_tramo(e, "TERRESTRE", "ARBUE", entorno["camionero"], AHORA + timedelta(hours=1),
                      AHORA + timedelta(hours=5))
    assert srv.obtener(e, id_operador=entorno["camionero"]).id_envio == e
    with pytest.raises(EntidadNoEncontrada):
        srv.obtener(e, id_operador=entorno["naviera"])


# ---------------------------------------------------------------- mapa y recorrido
def test_el_mapa_tiene_una_linea_por_contenedor_y_escapa_el_titulo():
    puntos = [{"fecha_hora": AHORA + timedelta(hours=h), "id_contenedor": k, "latitud": -34 + h, "longitud": -58.0}
              for h in range(3) for k in (1, 2)]
    pagina = html_mapa("Envío <5>", puntos)
    assert "Envío &lt;5&gt;" in pagina and pagina.count('"nombre": "Contenedor') == 2
    assert "leaflet@1.9.4" in pagina and "[-32, -58.0" in pagina                # el último punto


def test_mapa_de_un_envio(tmp_path):
    envio = Envio(5, 1, "ARBUE", "BRSSZ", AHORA, 1, 1)
    envios = SimpleNamespace(obtener=lambda i: envio if i == 5 else None)
    posiciones = {5: [{"fecha_hora": AHORA, "id_contenedor": 1, "latitud": -34.0, "longitud": -58.0}], 6: []}
    tel = SimpleNamespace(recorrido_envio=lambda i: posiciones.get(i, []))
    srv = ServicioTrazabilidad(envios, None, None, None, repo_telemetria=tel, carpeta_mapas=tmp_path)
    ruta = srv.mapa_envio(5)
    assert ruta == tmp_path / "envio_5.html" and "ARBUE" in ruta.read_text(encoding="utf-8")
    with pytest.raises(EntidadNoEncontrada):
        srv.mapa_envio(5, id_cliente=99)                                  # un cliente no ve envíos ajenos
    envio6 = Envio(6, 1, "ARBUE", "BRSSZ", AHORA, 1, 1)
    srv._envios = SimpleNamespace(obtener=lambda i: envio6)
    with pytest.raises(EntidadNoEncontrada, match="GPS"):
        srv.mapa_envio(6)


# ---------------------------------------------------------------- verificador
def test_control_de_reportes_cobrados():
    def control(pg, mongo):
        v = ServicioVerificacion(SimpleNamespace(reportes_en_facturas=lambda: set(pg)), *[None] * 10,
                                 repo_reportes=SimpleNamespace(ids_facturados=lambda: set(mongo)))
        c = Control("Reportes cobrados", "MongoDB ↔ PostgreSQL")
        v._reportes_cobrados(c)
        return c.estado
    assert control({"a"}, {"a"}) == OK
    assert control({"a", "b"}, {"a"}) == ADVERTENCIA                       # la próxima facturación lo marca
    assert control({"a"}, {"a", "c"}) == ERROR                              # marcado sin factura
