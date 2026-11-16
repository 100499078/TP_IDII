"""Prueba de punta a punta (fase 8): UN envío recorre los cinco motores, con los servicios reales.

Con un reloj simulado (hace 10 días, como en la carga de datos de prueba):
    PostgreSQL  crear el envío y su tramo, en tránsito, llegada, entregado, facturar y cobrar
    Cassandra   lecturas de GPS y temperatura durante el viaje (una fuera de rango)
    Redis       último contacto del sensor, aviso de la alerta, stream de sincronización
    MongoDB     alerta, incidente y línea de tiempo del envío
    Neo4j       el envío y su ruta, con la demora y el incidente contados en la relación RUTA
Al final comprueba en cada motor lo que tiene que haber y borra lo que creó. Usa ubicaciones,
stream y claves DE PRUEBA para no mezclarse con los datos reales. Necesita los cinco motores.
"""
import random
from datetime import datetime, time, timedelta, timezone

import pytest

from app.dominio.modelos import Sesion
from app.repositorios.cassandra.telemetria import RepositorioTelemetria
from app.repositorios.mongo.alertas import RepositorioAlertas
from app.repositorios.mongo.eventos import RepositorioEventos
from app.repositorios.mongo.incidentes import RepositorioIncidentes
from app.repositorios.neo4j.grafo import RepositorioGrafo
from app.repositorios.postgres.actores import RepositorioActores
from app.repositorios.postgres.catalogos import RepositorioCatalogos
from app.repositorios.postgres.comercial import RepositorioComercial
from app.repositorios.postgres.contenedores import RepositorioContenedores
from app.repositorios.postgres.envios import RepositorioEnvios
from app.repositorios.redis import cola_sync
from app.repositorios.redis import telemetria as redis_telemetria
from app.repositorios.redis.cache import RepositorioCache
from app.servicios.catalogos import ServicioCatalogos
from app.servicios.comercial import ServicioComercial
from app.servicios.contenedores import ServicioContenedores
from app.servicios.envios import ServicioEnvios
from app.servicios.sincronizacion import Proyeccion, ServicioSincronizacion
from app.servicios.telemetria import ServicioTelemetria
from app.servicios.trazabilidad import ProyeccionTrazabilidad, ServicioTrazabilidad
from tests.fakes import OutboxEnMemoria
from tests.integracion.test_operativa import cx, escenario, eventos_nuevos  # noqa: F401  (fixtures)

pytestmark = pytest.mark.integracion
ORIGEN, DESTINO = "TSTP2", "TSTP3"            # ubicaciones de prueba (no tocan las rutas reales)


class Reloj:
    def __init__(self, ahora):
        self.ahora = ahora

    def __call__(self):
        return self.ahora


@pytest.fixture
def motores(cx, monkeypatch):  # noqa: F811
    for nombre, prueba in (("MongoDB", lambda: cx.mongo.client.admin.command("ping")),
                           ("Neo4j", lambda: RepositorioGrafo(cx).verificar()),
                           ("Cassandra", lambda: RepositorioTelemetria(cx).ultimas_lecturas([0]))):
        try:
            prueba()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"{nombre} no disponible: {exc}")
    sufijo = random.randint(0, 10**9)
    claves = {"STREAM": f"test:sync:{sufijo}", "FALLIDOS": f"test:fallidos:{sufijo}"}
    for k, v in claves.items():
        monkeypatch.setattr(cola_sync, k, v)
    monkeypatch.setattr(redis_telemetria, "ALERTAS", f"test:alertas:{sufijo}")
    monkeypatch.setattr(redis_telemetria, "CONTACTO", f"test:contacto:{sufijo}")
    yield cx
    cx.redis.delete(*claves.values(), f"test:alertas:{sufijo}", f"test:contacto:{sufijo}")


def test_un_envio_por_los_cinco_motores(motores, escenario):  # noqa: F811
    cx = motores
    base = (datetime.now(timezone.utc) - timedelta(days=10)).replace(second=0, microsecond=0)
    reloj = Reloj(base)
    r_env, r_cat, r_cont, r_act = RepositorioEnvios(cx), RepositorioCatalogos(cx), RepositorioContenedores(cx), \
        RepositorioActores(cx)
    catalogos = ServicioCatalogos(r_cat, r_act)
    for codigo, lat in ((ORIGEN, -34.0), (DESTINO, -33.0)):
        if r_cat.ubicacion(codigo) is None:
            catalogos.crear_ubicacion(codigo, f"Prueba {codigo}", "CENTRO_DIST", lat, -60.0)
    contenedores = ServicioContenedores(r_cont)
    gps = contenedores.crear_sensor("GPS", "Test", "GPS-T", None, None)
    temp = contenedores.crear_sensor("TEMPERATURA", "Test", "TX-T", 2, 8)
    for s in (gps, temp):
        contenedores.instalar_sensor(s, escenario["contenedor"])

    envios = ServicioEnvios(r_env, r_act, r_cat, r_cont, reloj=reloj)
    telemetria = ServicioTelemetria(r_cont, RepositorioTelemetria(cx), redis_telemetria.RepositorioTelemetriaRedis(cx),
                                    RepositorioAlertas(cx), reloj=reloj)
    traz = ServicioTrazabilidad(r_env, r_cat, RepositorioEventos(cx), RepositorioIncidentes(cx), RepositorioCache(cx),
                                reloj=reloj)
    comercial = ServicioComercial(RepositorioComercial(cx), r_act, None, reloj=reloj)
    sesion = Sesion("s", 1, "admin@logistica.local", "Admin", ["ADMINISTRADOR"], 5, base, base)
    e = None
    try:
        # ---------------------------------------------------------------- la operación, hace 10 días
        e = envios.crear_envio(escenario["cliente"], ORIGEN, DESTINO, base + timedelta(days=2), 1000, 20,
                               [escenario["contenedor"]])
        t = envios.agregar_tramo(e, "TERRESTRE", DESTINO, escenario["operador"], base + timedelta(hours=1),
                                 base + timedelta(hours=20))
        reloj.ahora = base + timedelta(hours=1)
        envios.cambiar_estado(e, "EN_TRANSITO")
        alertas = []
        for h in range(2, 20, 3):                                   # lecturas cada 3 h durante el viaje
            reloj.ahora = base + timedelta(hours=h)
            telemetria.olvidar_cache()
            telemetria.registrar_lectura(gps, reloj.ahora, latitud=-34 + h / 20, longitud=-60.0)
            alertas += telemetria.registrar_lectura(temp, reloj.ahora, temperatura=12.0 if h == 8 else 5.0)
        reloj.ahora = base + timedelta(hours=9)
        id_inc = traz.registrar_incidente(e, "CLIMA", 4, "Tormenta en la ruta (prueba)", sesion)
        envios.registrar_llegada(t, base + timedelta(hours=22))     # 2 h tarde
        reloj.ahora = base + timedelta(hours=23)
        envios.cambiar_estado(e, "ENTREGADO")
        reloj.ahora = base + timedelta(days=2)
        factura = comercial.facturar_cliente(escenario["cliente"])
        reloj.ahora = base + timedelta(days=5)
        assert comercial.registrar_pago(factura.id_factura, factura.importe_total, "TRANSFERENCIA") == "PAGADA"

        # ---------------------------------------------------------------- la sincronización (stream de prueba)
        sync = ServicioSincronizacion(
            OutboxEnMemoria(eventos_nuevos(cx, escenario["ultimo_evento"])), cola_sync.RepositorioColaSync(cx),
            RepositorioGrafo(cx), repo_incidentes=RepositorioIncidentes(cx),
            otras=[Proyeccion("MongoDB", cola_sync.RepositorioColaSync(cx, cola_sync.GRUPO_TRAZABILIDAD),
                              ProyeccionTrazabilidad(RepositorioEventos(cx), r_env, r_cat))])
        for _ in range(20):
            if sync.ciclo() == (0, 0):
                break

        # ---------------------------------------------------------------- PostgreSQL
        envio = envios.obtener(e)
        assert envio.estado == "ENTREGADO" and envio.fecha_creacion == base           # la fecha la puso el reloj
        assert envio.fecha_entrega_real == base + timedelta(hours=22)
        assert comercial.factura(factura.id_factura).estado == "PAGADA"
        assert RepositorioComercial(cx).cuenta(escenario["cliente"])["saldo"] == 0
        with cx.transaccion() as cur:
            cur.execute("""SELECT m.tipo, m.fecha FROM movimiento_cc m JOIN cuenta_corriente cc USING (id_cuenta)
                            WHERE cc.id_actor = %s ORDER BY m.fecha""", (escenario["cliente"],))
            movimientos = [(f["tipo"], f["fecha"]) for f in cur.fetchall()]
        assert movimientos == [("DEBITO", datetime.combine((base + timedelta(days=2)).date(), time(12),
                                                           tzinfo=timezone.utc)),
                               ("CREDITO", base + timedelta(days=5))]                  # con la fecha del comprobante
        # ---------------------------------------------------------------- Cassandra y Redis
        ultima = {f["id_sensor"]: f for f in RepositorioTelemetria(cx).ultimas_lecturas([gps, temp])}
        assert ultima[temp]["fecha_hora"] == base + timedelta(hours=17)
        contacto = redis_telemetria.RepositorioTelemetriaRedis(cx).contactos()
        assert contacto[temp] == pytest.approx((base + timedelta(hours=17)).timestamp())
        assert len(RepositorioTelemetria(cx).recorrido_envio(e)) == 6                  # 6 posiciones GPS del envío
        # ---------------------------------------------------------------- MongoDB
        assert [a.tipo for a in alertas] == ["TEMPERATURA_ALTA"] and alertas[0].id_envio == e
        linea = RepositorioEventos(cx).de_envio(e)
        assert [x.tipo for x in linea] == ["CARGA_INICIADA", "EN_TRANSITO", "DESCARGA", "ENTREGADO"]
        assert linea[0].fecha_hora == base and linea[-1].codigo_ubicacion == DESTINO
        assert [i.id_incidente for i in traz.incidentes_de_envio(e)] == [id_inc]
        # ---------------------------------------------------------------- Neo4j
        grafo = RepositorioGrafo(cx)
        assert grafo.estados_envios()[e] == "ENTREGADO"
        ruta = grafo.estadisticas_rutas([{"origen": ORIGEN, "destino": DESTINO, "medio": "TERRESTRE"}])
        r = ruta[(ORIGEN, DESTINO, "TERRESTRE")]
        assert (r["envios"], r["incidentes"], r["demora_h"]) == (1, 1, 2.0)        # 1 viaje, 1 incidente, 2 h tarde
    finally:
        _limpiar(cx, escenario, e, [gps, temp])


def _limpiar(cx, escenario, e, sensores):  # noqa: F811
    cliente = escenario["cliente"]
    with cx.transaccion() as cur:
        cur.execute("""DELETE FROM movimiento_cc WHERE id_cuenta IN
                         (SELECT id_cuenta FROM cuenta_corriente WHERE id_actor = %s)""", (cliente,))
        cur.execute("DELETE FROM pago WHERE id_factura IN (SELECT id_factura FROM factura WHERE id_actor = %s)",
                    (cliente,))
        cur.execute("DELETE FROM item_factura WHERE id_factura IN (SELECT id_factura FROM factura WHERE id_actor = %s)",
                    (cliente,))
        cur.execute("DELETE FROM factura WHERE id_actor = %s", (cliente,))
        cur.execute("DELETE FROM envio WHERE id_cliente = %s", (cliente,))
        cur.execute("DELETE FROM sensor WHERE id_sensor = ANY(%s)", (sensores,))
        cur.execute("DELETE FROM ubicacion WHERE codigo IN (%s, %s)", (ORIGEN, DESTINO))
    if e is not None:
        for coleccion in ("eventos_logisticos", "incidentes", "alertas"):
            cx.mongo[coleccion].delete_many({"id_envio": e})
    try:
        with cx.neo4j.session() as s:
            s.run("""MATCH (n) WHERE (n:Envio AND n.id_envio = $e) OR (n:Ubicacion AND n.codigo IN [$o, $d])
                        OR ((n:Cliente OR n:Operador) AND n.id_actor IN [$c, $op])
                        OR (n:Contenedor AND n.id_contenedor = $k) OR (n:Pais AND n.codigo = 'TS')
                     OPTIONAL MATCH (n)-[:TIENE_TRAMO|TUVO_INCIDENTE]->(x)
                     DETACH DELETE n, x""", e=e, o=ORIGEN, d=DESTINO, c=cliente, op=escenario["operador"],
                  k=escenario["contenedor"]).consume()
    except Exception:  # noqa: BLE001   (sin Neo4j no hay nada que borrar ahí)
        pass
    # Cassandra: las lecturas quedan (son de un sensor y un envío que ya no existen; no molestan)
