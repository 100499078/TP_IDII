"""Integración de la fase 3 (IoT) contra los motores reales.

- test_redis_*: solo necesita Redis.
- test_lecturas_*: necesita PostgreSQL, Cassandra, MongoDB y Redis. Crea un contenedor y
  un sensor de prueba, registra lecturas reales y al final borra todo lo que creó.
Las alertas se publican en un stream DE PRUEBA para no aparecer en las consolas.
"""
import random
from datetime import datetime, timedelta, timezone

import pytest

from app.config import Config
from app.dominio.modelos import Sensor
from app.dominio.validaciones import validar_codigo_contenedor
from app.infra.conexiones import GestorConexiones
from app.repositorios.cassandra.telemetria import RepositorioTelemetria
from app.repositorios.mongo.alertas import RepositorioAlertas
from app.repositorios.postgres.contenedores import RepositorioContenedores
from app.repositorios.redis import telemetria as redis_telemetria
from app.servicios.contenedores import ServicioContenedores
from app.servicios.telemetria import ServicioTelemetria

pytestmark = pytest.mark.integracion


@pytest.fixture(scope="module")
def cx():
    gestor = GestorConexiones(Config.desde_entorno())
    yield gestor
    gestor.cerrar()


@pytest.fixture
def stream_de_prueba(cx, monkeypatch):
    try:
        cx.redis.ping()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Redis no disponible: {exc}")
    sufijo = random.randint(0, 10**9)
    monkeypatch.setattr(redis_telemetria, "ALERTAS", f"test:alertas:{sufijo}")
    monkeypatch.setattr(redis_telemetria, "CONTACTO", f"test:contacto:{sufijo}")
    yield
    cx.redis.delete(f"test:alertas:{sufijo}", f"test:contacto:{sufijo}")


def test_redis_contacto_silencio_y_stream(cx, stream_de_prueba):
    r = redis_telemetria.RepositorioTelemetriaRedis(cx)
    ahora = datetime.now(timezone.utc)
    r.registrar_contacto(1, ahora)
    r.registrar_contacto(1, ahora - timedelta(hours=1))              # más vieja: no atrasa (ZADD GT)
    r.registrar_contacto(2, ahora - timedelta(hours=2))
    assert r.contactos()[1] == pytest.approx(ahora.timestamp())
    assert set(r.sin_contacto_desde(ahora - timedelta(minutes=30))) == {2}
    sensor = 900_000 + random.randint(0, 99_999)
    try:
        assert r.reservar_alerta("TEMPERATURA_ALTA", sensor) is True
        assert r.reservar_alerta("TEMPERATURA_ALTA", sensor) is False   # silencio de 15 min
        assert 0 < cx.redis.ttl(f"alerta:TEMPERATURA_ALTA:{sensor}") <= redis_telemetria.SILENCIO_SEGUNDOS
    finally:
        r.liberar_alerta("TEMPERATURA_ALTA", sensor)
    r.publicar_alerta({"tipo": "PRUEBA", "mensaje": "hola", "id_envio": None})   # sin crear el grupo antes
    assert [a["tipo"] for a in r.alertas_nuevas("test")] == ["PRUEBA"]
    assert r.alertas_nuevas("test") == []                             # ya fue entregada y confirmada


def _codigo_contenedor() -> str:
    base = f"TSTU{random.randint(0, 999_999):06d}"
    return next(base + str(d) for d in range(10) if validar_codigo_contenedor(base + str(d)) is None)


@pytest.fixture
def sensor_de_prueba(cx, stream_de_prueba):
    try:
        cx.cassandra.execute("SELECT now() FROM system.local")
        cx.mongo.client.admin.command("ping")
        with cx.transaccion() as cur:
            cur.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Faltan motores (PostgreSQL, Cassandra o MongoDB): {exc}")
    srv = ServicioContenedores(RepositorioContenedores(cx))
    id_contenedor = srv.crear_contenedor(_codigo_contenedor(), "REEFER", 30)
    id_sensor = srv.crear_sensor("TEMPERATURA", "Prueba", None, 2, 8)
    srv.instalar_sensor(id_sensor, id_contenedor)
    yield id_sensor, id_contenedor
    dia = datetime.now(timezone.utc).date()
    for cql, valores in [("DELETE FROM lecturas_por_sensor WHERE id_sensor = %s AND dia = %s", (id_sensor, dia)),
                         ("DELETE FROM lecturas_por_contenedor WHERE id_contenedor = %s AND dia = %s",
                          (id_contenedor, dia)),
                         ("DELETE FROM ultima_lectura_por_sensor WHERE id_sensor = %s", (id_sensor,))]:
        cx.cassandra.execute(cql, valores)
    for f in RepositorioTelemetria(cx).anomalias_dia(dia, 1000):
        if f["id_sensor"] == id_sensor:
            cx.cassandra.execute("DELETE FROM anomalias_temperatura WHERE dia = %s AND fecha_hora = %s AND id_sensor = %s",
                                 (dia, f["fecha_hora"], id_sensor))
    cx.mongo["alertas"].delete_many({"origen.id_sensor": id_sensor})
    cx.redis.delete(f"alerta:TEMPERATURA_ALTA:{id_sensor}")
    with cx.transaccion() as cur:
        cur.execute("DELETE FROM sensor WHERE id_sensor = %s", (id_sensor,))
        cur.execute("DELETE FROM contenedor WHERE id_contenedor = %s", (id_contenedor,))
        cur.execute("DELETE FROM outbox WHERE agregado = 'contenedor' AND id_agregado = %s", (id_contenedor,))


def test_lecturas_en_cassandra_alerta_en_mongo_y_aviso_en_redis(cx, sensor_de_prueba):
    id_sensor, id_contenedor = sensor_de_prueba
    srv = ServicioTelemetria(RepositorioContenedores(cx), RepositorioTelemetria(cx),
                             redis_telemetria.RepositorioTelemetriaRedis(cx), RepositorioAlertas(cx))
    ahora = datetime.now(timezone.utc).replace(microsecond=0)
    if (ahora - timedelta(minutes=3)).date() != ahora.date():
        pytest.skip("Justo después de la medianoche UTC las lecturas caerían en dos días distintos")
    tarde = max(ahora - timedelta(hours=1), datetime.combine(ahora.date(), datetime.min.time(), timezone.utc))
    assert srv.registrar_lectura(id_sensor, ahora - timedelta(minutes=2), temperatura=5.0, bateria=90) == []
    alertas = srv.registrar_lectura(id_sensor, ahora - timedelta(minutes=1), temperatura=9.4, bateria=89)
    assert [a.tipo for a in alertas] == ["TEMPERATURA_ALTA"]
    srv.registrar_lectura(id_sensor, tarde, temperatura=4.0, bateria=95)   # una lectura vieja que llega tarde

    lecturas = srv.lecturas_sensor(id_sensor, ahora.date())
    assert [l["temperatura"] for l in lecturas][:2] == [9.4, 5.0]          # más recientes primero
    assert len(srv.lecturas_contenedor(id_contenedor, ahora.date())) == 3
    ultima = srv.ultimas_lecturas(id_contenedor)[0]["lectura"]
    assert ultima["temperatura"] == 9.4                                    # la tardía no pisó la última
    assert any(f["id_sensor"] == id_sensor for f in srv.anomalias(ahora.date()))
    doc = cx.mongo["alertas"].find_one({"origen.id_sensor": id_sensor})
    assert doc["tipo"] == "TEMPERATURA_ALTA" and doc["estado"] == "NUEVA" and doc["umbral"] == 8.0
    avisos = srv.alertas_nuevas("test")
    assert avisos and avisos[0]["id_alerta"] == str(doc["_id"])
