"""Simulador de sensores: posiciones sobre la ruta real y lecturas que pasan por el servicio."""
from datetime import timedelta

from app.dominio.modelos import Envio, Sensor, Tramo
from app.servicios.simulacion import SimuladorSensores, posicion_en_ruta
from app.servicios.telemetria import ServicioTelemetria
from tests.fakes import AHORA, AlertasEnMemoria, CassandraEnMemoria, SensoresEnMemoria, TelemetriaRedisEnMemoria

COORDS = {"AAAAA": (0.0, 0.0), "BBBBB": (10.0, 20.0), "CCCCC": (10.0, 40.0)}


def envio():
    t1 = Tramo(1, 1, 1, "TERRESTRE", "AAAAA", "BBBBB", 3, AHORA, AHORA + timedelta(hours=10))
    t2 = Tramo(2, 1, 2, "MARITIMO", "BBBBB", "CCCCC", 4, AHORA + timedelta(hours=12), AHORA + timedelta(hours=22))
    return Envio(1, 1, "AAAAA", "CCCCC", AHORA + timedelta(days=2), 100, 10, tramos=[t1, t2])


def test_posicion_interpolada_en_el_tramo_que_corresponde():
    e = envio()
    assert posicion_en_ruta(e, AHORA - timedelta(hours=1), COORDS.get) == (0.0, 0.0)    # antes de salir
    assert posicion_en_ruta(e, AHORA + timedelta(hours=5), COORDS.get) == (5.0, 10.0)   # mitad del tramo 1
    assert posicion_en_ruta(e, AHORA + timedelta(hours=11), COORDS.get) == (10.0, 20.0)  # esperando en BBBBB
    assert posicion_en_ruta(e, AHORA + timedelta(hours=17), COORDS.get) == (10.0, 30.0)  # mitad del tramo 2
    assert posicion_en_ruta(e, AHORA + timedelta(days=5), COORDS.get) == (10.0, 40.0)    # llegó


class _Envios:
    def obtener(self, id_envio):
        return envio()


class _Catalogos:
    def ubicacion(self, codigo):
        from app.dominio.modelos import Ubicacion
        la, lo = COORDS[codigo]
        return Ubicacion(None, codigo, codigo, "PUERTO", codigo[:2], la, lo)


def test_lote_genera_lecturas_para_cada_sensor_y_alertas_con_anomalias():
    sensores = SensoresEnMemoria([Sensor(1, "TEMPERATURA", "A", temp_min=2, temp_max=8, id_contenedor=10),
                                  Sensor(2, "GPS", "A", id_contenedor=10)], {10: 1})
    cass, mongo = CassandraEnMemoria(), AlertasEnMemoria()
    reloj = lambda: AHORA + timedelta(hours=5)              # noqa: E731  (el mismo "ahora" para los dos)
    telemetria = ServicioTelemetria(sensores, cass, TelemetriaRedisEnMemoria(), mongo, reloj=reloj)
    sim = SimuladorSensores(telemetria, sensores, _Envios(), _Catalogos(), prob_anomalia=1.0, semilla=7,
                            reloj=reloj)
    r = sim.lote(horas=2, cada_minutos=30)                   # 5 momentos × 2 sensores
    assert (r.lecturas, r.errores) == (10, 0)
    assert any(l.latitud is not None for l in cass.lecturas if l.id_sensor == 2)
    assert all(not 2 <= l.temperatura <= 8 for l in cass.lecturas if l.id_sensor == 1)   # todas anómalas
    tipos = [a.tipo for a in mongo.alertas]                  # 5 lecturas anómalas, pero como mucho
    assert 1 <= len(tipos) == len(set(tipos)) <= 2           # una alerta por tipo (silencio de 15 min)
