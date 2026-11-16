"""Fase 7: procesos periódicos, predicción de riesgo y reportes (con dobles en memoria)."""
import csv
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.excepciones import EntidadNoEncontrada, ErrorConflicto, ErrorValidacion
from app.servicios.procesos import Proceso, ServicioProcesos, mes_anterior
from app.servicios.reportes import ServicioReportes, para_mongo
from app.servicios.riesgo import calcular, nivel_de
from tests.fakes import AHORA


# ---------------------------------------------------------------- procesos
class EstadoEnMemoria:
    def __init__(self):
        self.datos = {}

    def registrar(self, nombre, inicio, fin, ok, resultado):
        self.datos[nombre] = {"inicio": inicio, "fin": fin, "ok": ok, "resultado": resultado}

    def ultimo(self, nombre):
        return self.datos.get(nombre)


class LocksEnMemoria:
    def __init__(self):
        self.tomados = set()

    def tomar(self, nombre, segundos):
        if nombre in self.tomados:
            return None
        self.tomados.add(nombre)
        return "t"

    def soltar(self, nombre, token):
        self.tomados.discard(nombre)
        return True


def armar_procesos(reloj):
    llamadas = []

    def falla():
        raise RuntimeError("MongoDB caído")
    procesos = [Proceso("rapido", "", timedelta(seconds=10), lambda: llamadas.append(1) or "ok"),
                Proceso("diario", "", timedelta(days=1), lambda: "listo"),
                Proceso("roto", "", timedelta(minutes=5), falla)]
    estado, locks = EstadoEnMemoria(), LocksEnMemoria()
    return ServicioProcesos(procesos, estado, locks, reloj=reloj), estado, locks, llamadas


def test_un_proceso_corre_cuando_le_toca():
    ahora = [AHORA]
    srv, estado, locks, llamadas = armar_procesos(lambda: ahora[0])
    assert set(srv.pendientes()) == {"rapido", "diario", "roto"}       # nunca corrieron
    for nombre in srv.pendientes():
        srv.ejecutar(nombre)
    assert srv.pendientes() == []
    ahora[0] += timedelta(seconds=11)
    assert srv.pendientes() == ["rapido"]                               # solo el de 10 s
    ahora[0] += timedelta(days=1)
    assert set(srv.pendientes()) == {"rapido", "diario", "roto"}


def test_un_proceso_que_falla_queda_registrado_y_suelta_el_lock():
    srv, estado, locks, _ = armar_procesos(lambda: AHORA)
    r = srv.ejecutar("roto")
    assert not r.ok and "MongoDB caído" in r.resultado
    assert estado.datos["roto"]["ok"] is False and locks.tomados == set()


def test_si_el_lock_esta_tomado_no_corre_dos_veces():
    srv, estado, locks, llamadas = armar_procesos(lambda: AHORA)
    locks.tomados.add("periodico:rapido")                               # otro planificador lo está corriendo
    r = srv.ejecutar("rapido")
    assert not r.ok and "lock" in r.resultado and llamadas == []


@pytest.mark.parametrize("hoy,esperado", [(datetime(2026, 10, 29, tzinfo=timezone.utc), "2026-09"),
                                         (datetime(2027, 1, 1, 3, tzinfo=timezone.utc), "2026-12")])
def test_mes_anterior(hoy, esperado):
    periodo, desde, hasta = mes_anterior(hoy)
    assert periodo == esperado and desde.day == 1 and hasta.day == 1 and hasta > desde


# ---------------------------------------------------------------- riesgo
def envio(**cambios):
    e = {"id_envio": 1, "estado": "EN_TRANSITO", "fecha_entrega_estimada": AHORA + timedelta(days=10),
         "llegada_planificada": AHORA + timedelta(days=5),
         "tramos_pendientes": [{"origen": "ARBUE", "destino": "NLRTM", "medio": "MARITIMO"}]}
    e.update(cambios)
    return e


def test_riesgo_bajo_sin_senales():
    p = calcular(envio(), {}, None, AHORA)
    assert p["nivel"] == "BAJO" and p["riesgo_climatico"] == 0.3 and p["modelo"]["version"] == "1.0"


def test_riesgo_sube_con_incidentes_y_ruta_problematica():
    rutas = {("ARBUE", "NLRTM", "MARITIMO"): {"envios": 4, "incidentes": 2, "demora_h": 36.0}}
    p = calcular(envio(estado="DEMORADO"), rutas, {"abiertos": 2, "tipos": ["CLIMA"]}, AHORA)
    assert p["variables"]["tasa_incidentes_ruta"] == 0.5
    assert p["riesgo_operativo"] == 0.85                     # 0.5·0.5 + 0.15·2 + 0.3
    assert p["riesgo_climatico"] == 0.5                      # 0.3 + 0.2 por CLIMA
    assert p["nivel"] in ("ALTO", "CRITICO")


def test_si_ya_paso_la_fecha_la_demora_es_segura():
    p = calcular(envio(fecha_entrega_estimada=AHORA - timedelta(hours=1)), {}, None, AHORA)
    assert p["probabilidad_demora"] == 1.0 and p["nivel"] == "CRITICO" and p["variables"]["ya_atrasado"]


def test_si_lo_planificado_llega_tarde_sube_el_riesgo():
    tarde = calcular(envio(llegada_planificada=AHORA + timedelta(days=12)), {}, None, AHORA)
    a_tiempo = calcular(envio(), {}, None, AHORA)
    assert round(tarde["probabilidad_demora"] - a_tiempo["probabilidad_demora"], 3) == 0.3


def test_niveles():
    assert [nivel_de(x) for x in (0.1, 0.3, 0.6, 0.9)] == ["BAJO", "MEDIO", "ALTO", "CRITICO"]


# ---------------------------------------------------------------- reportes
class ColaReportesEnMemoria:
    def __init__(self):
        self.docs = {}

    def solicitar(self, id_usuario, tipo, parametros, formato, fecha):
        i = f"{len(self.docs) + 1:024x}"
        self.docs[i] = {"id_reporte": i, "id_usuario": id_usuario, "tipo": tipo, "parametros": parametros,
                        "formato": formato, "estado": "SOLICITADO", "fecha_solicitud": fecha}
        return i

    def tomar_siguiente(self, ahora, id_reporte=None):
        for d in sorted(self.docs.values(), key=lambda d: d["fecha_solicitud"]):
            if d["estado"] == "SOLICITADO" and id_reporte in (None, d["id_reporte"]):
                d["estado"] = "EN_PROCESO"
                return dict(d)
        return None

    def guardar_resultado(self, i, filas, ahora):
        self.docs[i].update(estado="GENERADO", resultado=filas)

    def marcar_error(self, i, error, ahora):
        self.docs[i].update(estado="ERROR", error=error)

    def liberar_trabados(self, ahora, minutos=10):
        return 0

    def obtener(self, i):
        return self.docs.get(i)

    def de_usuario(self, u):
        return [d for d in self.docs.values() if d["id_usuario"] == u]


class SQLFalso:
    def __getattr__(self, nombre):
        if nombre == "costos_transporte":
            return lambda desde, hasta, id_cliente=None: (_ for _ in ()).throw(RuntimeError("consulta rota"))
        return lambda desde, hasta, id_cliente=None: [{"estado": "ENTREGADO", "envios": 3, "total": Decimal("10.50"),
                                      "desde": desde}]


class Incidentes:
    def por_tipo(self, id_cliente=None):
        return [{"tipo": "CLIMA", "abiertos": 2}]

    def ranking_rutas(self, n):
        return []


class Grafo:
    def rutas_problematicas(self, n):
        return [{"desde": "ARBUE", "hasta": "NLRTM", "incidentes": 1}]


@pytest.fixture
def rep(tmp_path):
    cola = ColaReportesEnMemoria()
    return ServicioReportes(cola, SQLFalso(), Incidentes(), Grafo(), tmp_path, reloj=lambda: AHORA), cola, tmp_path


def test_cola_de_reportes_genera_y_un_error_no_frena_a_los_demas(rep):
    srv, cola, _ = rep
    a = srv.solicitar(7, "ENVIOS_POR_ESTADO", desde=AHORA.date())
    b = srv.solicitar(7, "COSTOS_TRANSPORTE")
    c = srv.solicitar(7, "RANKING_RUTAS")
    assert srv.procesar_pendientes() == "2 reportes generados, 1 con error"
    assert cola.docs[a]["estado"] == "GENERADO" and cola.docs[a]["resultado"][0]["total"] == 10.5   # Decimal -> float
    assert cola.docs[a]["resultado"][0]["desde"] == datetime(2026, 11, 1, tzinfo=timezone.utc)   # date -> datetime
    assert cola.docs[b]["estado"] == "ERROR" and "consulta rota" in cola.docs[b]["error"]
    assert cola.docs[c]["estado"] == "GENERADO"


def test_reportes_invalidos_y_ajenos(rep):
    srv, cola, _ = rep
    with pytest.raises(ErrorValidacion):
        srv.solicitar(7, "INVENTADO")
    with pytest.raises(ErrorValidacion):
        srv.solicitar(7, "ENVIOS_POR_ESTADO", desde=AHORA.date(), hasta=AHORA.date() - timedelta(days=1))
    i = srv.solicitar(7, "ENVIOS_POR_ESTADO")
    with pytest.raises(EntidadNoEncontrada):
        srv.obtener(i, id_usuario=8)                              # de otro usuario
    assert srv.obtener(i, id_usuario=8, es_supervisor=True)["id_reporte"] == i


def test_exportar_csv_y_json(rep):
    srv, cola, carpeta = rep
    i = srv.solicitar(7, "INCIDENTES_ABIERTOS")
    with pytest.raises(ErrorConflicto):
        srv.exportar(cola.docs[i], "CSV")                         # todavía no está generado
    srv.procesar_pendientes()
    ruta_csv = srv.exportar(cola.docs[i], "CSV")
    with ruta_csv.open(encoding="utf-8-sig") as f:
        assert list(csv.reader(f, delimiter=";")) == [["tipo", "abiertos"], ["CLIMA", "2"]]
    assert json.loads(srv.exportar(cola.docs[i], "JSON").read_text(encoding="utf-8")) == [{"tipo": "CLIMA", "abiertos": 2}]


def test_para_mongo_convierte_decimal_y_fechas():
    from datetime import date
    assert para_mongo({"a": Decimal("1.5"), "b": [date(2026, 1, 2)]}) == {
        "a": 1.5, "b": [datetime(2026, 1, 2, tzinfo=timezone.utc)]}
