"""Fase 8: verificador de consistencia, reportes de dos motores y los ajustes de la fase (sin bases)."""
from datetime import date, datetime, time, timezone
from types import SimpleNamespace

from app.dominio.modelos import Sensor
from app.repositorios.postgres.comercial import momento_del_asiento
from app.servicios.reportes import desempeno_historico, promedio_incidentes
from app.servicios.telemetria import ServicioTelemetria
from app.servicios.verificacion import ADVERTENCIA, ERROR, OK, SIN_DATOS, ServicioVerificacion
from tests.fakes import AHORA, CassandraEnMemoria, SensoresEnMemoria, TelemetriaRedisEnMemoria


# ---------------------------------------------------------------- dobles mínimos
class Controles:
    def __init__(self, estados, **filas):
        self.estados, self.filas = estados, filas

    def estados_envios(self):
        return dict(self.estados)

    def __getattr__(self, nombre):                      # cuentas_descuadradas, facturas_descuadradas, ...
        return lambda: self.filas.get(nombre, [])


class Cola:
    def __init__(self, sin_confirmar=0, fallidos=0):
        self.e = {"sin_confirmar": sin_confirmar, "fallidos": fallidos, "en_stream": 0}

    def estado(self):
        return self.e


class Contador:
    def __init__(self, n=0):
        self.n = n

    def contar_pendientes(self):
        return self.n


class Grafo:
    def __init__(self, estados, incidentes=0, caido=False):
        self.estados, self.incidentes, self.caido = estados, incidentes, caido

    def estados_envios(self):
        if self.caido:
            raise ConnectionError("Neo4j caído")
        return dict(self.estados)

    def contar_incidentes(self):
        return self.incidentes


class Eventos:
    def __init__(self, por_tipo):
        self.por_tipo = por_tipo

    def envios_con_evento(self, tipo):
        return set(self.por_tipo.get(tipo, ()))


class Incidentes:
    def __init__(self, n=0, pendientes=0):
        self.n, self.pendientes = n, pendientes

    def contar(self):
        return self.n

    def contar_pendientes_grafo(self):
        return self.pendientes


def armar(pg=None, neo=None, eventos=None, outbox=0, fallidos=0, grafo_caido=False, **filas):
    pg = pg if pg is not None else {1: "ENTREGADO", 2: "EN_TRANSITO"}
    neo = neo if neo is not None else dict(pg)
    eventos = eventos if eventos is not None else {"CARGA_INICIADA": pg, "ENTREGADO": [i for i, e in pg.items()
                                                                                       if e == "ENTREGADO"]}
    sesiones = SimpleNamespace(existe=lambda i: True)
    return ServicioVerificacion(
        Controles(pg, **filas), Contador(outbox), [("Neo4j", Cola(fallidos=fallidos)), ("MongoDB", Cola())],
        Grafo(neo, caido=grafo_caido), Eventos(eventos), Incidentes(), CassandraEnMemoria(),
        TelemetriaRedisEnMemoria(), SensoresEnMemoria([]), SimpleNamespace(activas=lambda: []), sesiones)


def estado(controles, nombre):
    return next(c for c in controles if c.nombre == nombre)


# ---------------------------------------------------------------- verificador
def test_todo_consistente_da_todo_ok():
    controles = armar().verificar()
    assert len(controles) == 11 and {c.estado for c in controles} == {OK}


def test_envio_que_falta_en_neo4j_es_error_si_no_hay_nada_en_camino():
    c = estado(armar(neo={1: "ENTREGADO"}).verificar(), "Envíos en el grafo")
    assert c.estado == ERROR and c.ejemplos == [2]


def test_si_hay_eventos_en_camino_es_solo_advertencia():
    controles = armar(neo={1: "ENTREGADO", 2: "CREADO"}, outbox=3).verificar()
    assert estado(controles, "Envíos en el grafo").estado == ADVERTENCIA        # consistencia eventual
    assert estado(controles, "Outbox publicado").estado == ADVERTENCIA


def test_eventos_fallidos_y_motor_caido():
    controles = armar(fallidos=2, grafo_caido=True).verificar()
    assert estado(controles, "Stream de sincronización").estado == ERROR
    assert estado(controles, "Envíos en el grafo").estado == SIN_DATOS         # los demás controles siguen
    assert estado(controles, "Saldos de cuentas corrientes").estado == OK


def test_entregado_sin_evento_en_mongo_y_saldo_descuadrado():
    controles = armar(eventos={"CARGA_INICIADA": [1, 2]},
                      cuentas_descuadradas=[{"razon_social": "Cliente SA"}]).verificar()
    assert estado(controles, "Línea de tiempo de los envíos").ejemplos == [1]
    c = estado(controles, "Saldos de cuentas corrientes")
    assert c.estado == ERROR and c.ejemplos == ["Cliente SA"]


# ---------------------------------------------------------------- reportes de dos motores
def test_promedio_de_incidentes_une_postgres_y_mongo():
    envios = [{"id_envio": 1, "estado": "ENTREGADO", "cliente": "A", "pais_origen": "AR", "pais_destino": "BR"},
              {"id_envio": 2, "estado": "ENTREGADO", "cliente": "A", "pais_origen": "AR", "pais_destino": "BR"},
              {"id_envio": 3, "estado": "DEMORADO", "cliente": "B", "pais_origen": "AR", "pais_destino": "NL"}]
    filas = promedio_incidentes(envios, {1: 3, 3: 1})                         # el 2 no tuvo incidentes
    por = {(f["agrupado_por"], f["valor"]): f for f in filas}
    assert por[("PAIS", "AR → BR")] == {"agrupado_por": "PAIS", "valor": "AR → BR", "envios": 2, "incidentes": 3,
                                        "promedio": 1.5}
    assert por[("CLIENTE", "B")]["promedio"] == 1.0 and por[("ESTADO", "ENTREGADO")]["incidentes"] == 3


def test_desempeno_historico_suma_los_incidentes_del_mes():
    meses = [{"mes": "2026-08", "envios": 10}, {"mes": "2026-09", "envios": 0}]
    filas = desempeno_historico(meses, {"2026-08": {"incidentes": 4}})
    assert [(f["incidentes"], f["incidentes_por_envio"]) for f in filas] == [(4, 0.4), (0, 0.0)]


# ---------------------------------------------------------------- ajustes de la fase 8
def test_el_asiento_lleva_la_fecha_del_comprobante():
    pago = datetime(2026, 5, 3, 15, 30, tzinfo=timezone.utc)
    assert momento_del_asiento(pago) == pago                                  # un pago: su fecha y hora
    assert momento_del_asiento(date(2026, 5, 1)) == datetime.combine(date(2026, 5, 1), time(12), tzinfo=timezone.utc)
    assert momento_del_asiento(datetime.now(timezone.utc).date()) is None    # de hoy: now() en la base
    assert momento_del_asiento(None) is None


def test_sensor_inactivo_solo_si_su_contenedor_esta_viajando():
    sensores = [Sensor(1, "TEMPERATURA", "X", None, id_contenedor=10, temp_min=2, temp_max=8),
                Sensor(2, "GPS", "X", None, id_contenedor=20)]
    pg = SensoresEnMemoria(sensores, envios_por_contenedor={10: 77})          # el contenedor 20 está en el depósito
    srv = ServicioTelemetria(pg, CassandraEnMemoria(), TelemetriaRedisEnMemoria(), _AlertasMongo(),
                             reloj=lambda: AHORA)
    alertas = srv.alertar_sin_contacto(30)
    assert [(a.id_sensor, a.id_envio) for a in alertas] == [(1, 77)]
    assert {f["sensor"].id_sensor for f in srv.sensores_sin_contacto(30)} == {1, 2}   # la consulta los muestra igual


class _AlertasMongo:
    def __init__(self):
        self.docs = []

    def insertar(self, a):
        a.id_alerta = f"{len(self.docs):024x}"
        self.docs.append(a)
        return a.id_alerta

