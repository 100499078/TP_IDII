"""Telemetría IoT: lecturas, anomalías, alertas y tolerancia a fallos (con dobles en memoria)."""
from datetime import timedelta

import pytest

from app.dominio.modelos import Sensor
from app.excepciones import ErrorConexion, ErrorConflicto, ErrorValidacion
from app.servicios.telemetria import ServicioTelemetria
from tests.fakes import AHORA, AlertasEnMemoria, CassandraEnMemoria, SensoresEnMemoria, TelemetriaRedisEnMemoria


def armar(cassandra=None, redis=None, mongo=None):
    sensores = SensoresEnMemoria([
        Sensor(1, "TEMPERATURA", "Acme", temp_min=2, temp_max=8, id_contenedor=10),
        Sensor(2, "GPS", "Acme", id_contenedor=10),
        Sensor(3, "TEMPERATURA", "Acme", temp_min=2, temp_max=8),                    # sin instalar
        Sensor(4, "TEMPERATURA", "Acme", temp_min=2, temp_max=8, id_contenedor=11, estado="FALLA"),
    ], envios_por_contenedor={10: 500})
    c, r, m = cassandra or CassandraEnMemoria(), redis or TelemetriaRedisEnMemoria(), mongo or AlertasEnMemoria()
    return ServicioTelemetria(sensores, c, r, m, reloj=lambda: AHORA), c, r, m


def test_lectura_normal_se_guarda_con_su_envio_y_contacto():
    srv, c, r, m = armar()
    assert srv.registrar_lectura(1, AHORA, temperatura=5.0, bateria=90) == []
    l = c.lecturas[0]
    assert (l.id_contenedor, l.id_envio, l.dia) == (10, 500, AHORA.date())
    assert r.contacto[1] == AHORA.timestamp()
    assert m.alertas == [] and c.anomalias == []


def test_temperatura_alta_genera_anomalia_y_alerta_en_mongo_y_redis():
    srv, c, r, m = armar()
    alertas = srv.registrar_lectura(1, AHORA, temperatura=9.5, bateria=90)
    assert [a.tipo for a in alertas] == ["TEMPERATURA_ALTA"]
    assert len(c.anomalias) == 1
    assert m.alertas[0].umbral == 8 and m.alertas[0].id_envio == 500
    assert r.stream[0]["id_alerta"] == m.alertas[0].id_alerta     # el aviso apunta al documento


def test_la_misma_alerta_no_se_repite_en_cada_lectura():
    srv, c, r, m = armar()
    srv.registrar_lectura(1, AHORA, temperatura=9.5)
    srv.registrar_lectura(1, AHORA + timedelta(minutes=1), temperatura=9.9)
    assert len(c.anomalias) == 2          # cada lectura fuera de rango queda registrada...
    assert len(m.alertas) == 1            # ...pero se avisa una sola vez (silencio en Redis)


def test_bateria_baja_y_temperatura_baja_son_dos_alertas():
    srv, *_ = armar()
    tipos = {a.tipo for a in srv.registrar_lectura(1, AHORA, temperatura=0.5, bateria=10)}
    assert tipos == {"TEMPERATURA_BAJA", "BATERIA_BAJA"}


def test_sensor_no_instalado_o_en_falla_no_acepta_lecturas():
    srv, *_ = armar()
    with pytest.raises(ErrorConflicto, match="no está instalado"):
        srv.registrar_lectura(3, AHORA, temperatura=5)
    with pytest.raises(ErrorConflicto, match="FALLA"):
        srv.registrar_lectura(4, AHORA, temperatura=5)


@pytest.mark.parametrize("valores", [{"humedad": 120}, {"bateria": -1}, {"latitud": 10.0}, {}])
def test_valores_invalidos(valores):
    srv, *_ = armar()
    with pytest.raises(ErrorValidacion):
        srv.registrar_lectura(2, AHORA, **valores)


def test_si_cassandra_esta_caida_la_lectura_no_se_registra():
    srv, c, r, m = armar(cassandra=CassandraEnMemoria(caida=True))
    with pytest.raises(ErrorConexion):
        srv.registrar_lectura(1, AHORA, temperatura=9.5)
    assert r.contacto == {} and m.alertas == []          # nada a medias


def test_si_redis_esta_caido_la_lectura_igual_se_guarda():
    srv, c, r, m = armar(redis=TelemetriaRedisEnMemoria(caido=True))
    srv.registrar_lectura(1, AHORA, temperatura=9.5)
    assert len(c.lecturas) == 1 and len(m.alertas) == 1   # sin control de repetición, pero sin perder datos


def test_si_mongo_falla_se_libera_el_silencio_para_reintentar():
    mongo = AlertasEnMemoria(caida=True)
    srv, c, r, m = armar(mongo=mongo)
    assert srv.registrar_lectura(1, AHORA, temperatura=9.5) == []
    assert r.silencios == set()                           # la próxima lectura vuelve a intentar
    mongo.caida = False
    assert len(srv.registrar_lectura(1, AHORA + timedelta(minutes=1), temperatura=9.6)) == 1


def test_sensores_sin_contacto():
    srv, c, r, m = armar()
    srv.registrar_lectura(1, AHORA - timedelta(minutes=30), temperatura=5)
    srv.registrar_lectura(2, AHORA - timedelta(minutes=2), bateria=80)
    sin = {f["sensor"].id_sensor for f in srv.sensores_sin_contacto(10)}
    assert sin == {1}                                     # el 3 no está instalado y el 4 está en FALLA


def test_lectura_vieja_no_atrasa_el_ultimo_contacto():
    srv, c, r, m = armar()
    srv.registrar_lectura(1, AHORA, temperatura=5)
    srv.registrar_lectura(1, AHORA - timedelta(hours=3), temperatura=5)
    assert r.contacto[1] == AHORA.timestamp()


def test_alertas_cambian_de_estado():
    srv, c, r, m = armar()
    a = srv.registrar_lectura(1, AHORA, temperatura=9.5)[0]
    srv.cambiar_estado_alerta(a.id_alerta, "RESUELTA")
    assert srv.alertas() == []
