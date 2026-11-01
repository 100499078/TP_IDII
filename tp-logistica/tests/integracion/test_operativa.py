"""Integración de la fase 2 contra PostgreSQL, Redis y (si está) Neo4j reales.

- Envío completo con los servicios y repositorios reales: verifica que cada cambio
  dejó su evento en el outbox, en la misma transacción.
- Esos eventos pasan por un stream de Redis DE PRUEBA (no el real) para no
  quitarle eventos al sincronizador de la aplicación.
- Si Neo4j responde, aplica eventos con IDs altos, comprueba la ruta y que
  reprocesar no duplica, y borra lo que creó.
Todo lo creado en PostgreSQL se borra al final.
"""
import random
import re
from datetime import datetime, timedelta, timezone

import pytest

from app.config import Config
from app.dominio.modelos import Actor
from app.dominio.validaciones import calcular_digito_cuit
from app.infra.conexiones import GestorConexiones
from app.repositorios.neo4j.grafo import RepositorioGrafo
from app.repositorios.postgres.actores import RepositorioActores
from app.repositorios.postgres.catalogos import RepositorioCatalogos
from app.repositorios.postgres.contenedores import RepositorioContenedores
from app.repositorios.postgres.envios import RepositorioEnvios
from app.repositorios.redis import cola_sync
from app.servicios.actores import ServicioActores
from app.servicios.contenedores import ServicioContenedores
from app.servicios.envios import ServicioEnvios
from app.servicios.sincronizacion import ServicioSincronizacion
from tests.fakes import GrafoEnMemoria, OutboxEnMemoria

pytestmark = pytest.mark.integracion


@pytest.fixture(scope="module")
def cx():
    gestor = GestorConexiones(Config.desde_entorno())
    try:
        gestor.redis.ping()
        with gestor.transaccion() as cur:
            cur.execute("SELECT 1 FROM ubicacion WHERE codigo IN ('ARROS', 'ARBUE', 'NLRTM') HAVING count(*) = 3")
            if cur.fetchone() is None:
                pytest.skip("Faltan las ubicaciones iniciales: ejecutá herramientas.inicializar_bases")
    except pytest.skip.Exception:
        raise
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL o Redis no disponibles: {exc}")
    yield gestor
    gestor.cerrar()


def cuit_al_azar() -> str:
    while True:
        diez = f"30{random.randint(10_000_000, 99_999_999)}"
        digito = calcular_digito_cuit(diez)
        if digito is not None:
            return f"{diez[:2]}-{diez[2:]}-{digito}"


def codigo_contenedor_al_azar() -> str:
    from app.dominio.validaciones import validar_codigo_contenedor
    base = f"TSTU{random.randint(0, 999_999):06d}"
    for control in range(10):
        if validar_codigo_contenedor(base + str(control)) is None:
            return base + str(control)
    raise AssertionError("no se encontró dígito de control")


@pytest.fixture
def escenario(cx):
    """Crea cliente, operador y contenedor reales; al final borra todo lo creado."""
    with cx.transaccion() as cur:
        cur.execute("SELECT coalesce(max(id_evento), 0) AS m FROM outbox")
        ultimo_evento = cur.fetchone()["m"]
    actores = ServicioActores(RepositorioActores(cx))
    cliente = actores.crear(Actor(None, "CLIENTE", "Cliente Prueba SA", cuit_al_azar(), "cp@test.local", "AR"))
    operador = actores.crear(Actor(None, "OPERADOR", "Operador Prueba SA", cuit_al_azar(), "op@test.local", "AR",
                                   medios=["TERRESTRE", "MARITIMO"]))
    contenedor = ServicioContenedores(RepositorioContenedores(cx)).crear_contenedor(
        codigo_contenedor_al_azar(), "DRY", 60)
    datos = dict(cliente=cliente, operador=operador, contenedor=contenedor, ultimo_evento=ultimo_evento)
    yield datos
    with cx.transaccion() as cur:
        cur.execute("DELETE FROM envio WHERE id_cliente = %s", (cliente,))          # cascada: tramos y contenedores
        cur.execute("DELETE FROM contenedor WHERE id_contenedor = %s", (contenedor,))
        cur.execute("DELETE FROM cuenta_corriente WHERE id_actor IN (%s, %s)", (cliente, operador))
        cur.execute("DELETE FROM actor WHERE id_actor IN (%s, %s)", (cliente, operador))  # cascada: subtipos
        cur.execute("DELETE FROM outbox WHERE id_evento > %s", (ultimo_evento,))


def eventos_nuevos(cx, desde: int) -> list[dict]:
    with cx.transaccion() as cur:
        cur.execute("SELECT id_evento, tipo_evento, payload FROM outbox WHERE id_evento > %s ORDER BY id_evento",
                    (desde,))
        return cur.fetchall()


def test_envio_completo_deja_sus_eventos_en_el_outbox(cx, escenario, monkeypatch):
    srv = ServicioEnvios(RepositorioEnvios(cx), RepositorioActores(cx), RepositorioCatalogos(cx),
                         RepositorioContenedores(cx))
    ahora = datetime.now(timezone.utc)
    e = srv.crear_envio(escenario["cliente"], "ARROS", "NLRTM", ahora + timedelta(days=20), 500, 30,
                        [escenario["contenedor"]])
    t1 = srv.agregar_tramo(e, "TERRESTRE", "ARBUE", escenario["operador"], ahora + timedelta(hours=1),
                           ahora + timedelta(hours=6))
    t2 = srv.agregar_tramo(e, "MARITIMO", "NLRTM", escenario["operador"], ahora + timedelta(hours=8),
                           ahora + timedelta(days=15))
    srv.cambiar_estado(e, "EN_TRANSITO")
    srv.registrar_llegada(t1, ahora + timedelta(hours=7))
    srv.registrar_llegada(t2, ahora + timedelta(days=14))
    assert srv.cambiar_estado(e, "ENTREGADO") == [escenario["contenedor"]]

    eventos = eventos_nuevos(cx, escenario["ultimo_evento"])
    tipos = [ev["tipo_evento"] for ev in eventos]
    assert tipos[-7:] == ["ENVIO_CREADO", "TRAMO_AGREGADO", "TRAMO_AGREGADO", "ENVIO_ESTADO",
                          "TRAMO_LLEGADA", "TRAMO_LLEGADA", "ENVIO_ESTADO"]
    assert eventos[-3]["payload"]["demora_h"] == 1.0                  # el tramo 1 llegó 1 h tarde
    assert eventos[-2]["payload"]["demora_h"] == -24.0                # el tramo 2, un día antes

    # Cada parámetro $x de las sentencias Cypher tiene que venir en el payload del evento
    for ev in eventos:
        sentencias = RepositorioGrafo.sentencias_de(ev["tipo_evento"], ev["payload"])
        faltan = set(re.findall(r"\$(\w+)", " ".join(sentencias))) - set(ev["payload"])
        assert not faltan, (ev["tipo_evento"], faltan)

    # Pasan por un stream de Redis de prueba, en orden, y quedan confirmados
    sufijo = random.randint(0, 10**9)
    monkeypatch.setattr(cola_sync, "STREAM", f"test:sync:{sufijo}")
    monkeypatch.setattr(cola_sync, "FALLIDOS", f"test:fallidos:{sufijo}")
    cola = cola_sync.RepositorioColaSync(cx)
    grafo = GrafoEnMemoria()
    try:
        servicio = ServicioSincronizacion(OutboxEnMemoria(eventos), cola, grafo)
        assert servicio.ciclo() == (len(eventos), len(eventos))
        assert [t for t, _ in grafo.aplicados] == tipos
        assert cola.estado()["sin_confirmar"] == 0
    finally:
        cx.redis.delete(cola_sync.STREAM, cola_sync.FALLIDOS)


def test_neo4j_ruta_e_idempotencia(cx):
    try:
        cx.neo4j.verify_connectivity()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Neo4j no disponible: {exc}")
    grafo = RepositorioGrafo(cx)
    base = 9_000_000 + random.randint(0, 99_999)
    ahora = datetime.now(timezone.utc).isoformat()
    eventos = [
        ("ACTOR_GUARDADO", {"id_actor": base, "tipo": "CLIENTE", "razon_social": "Cliente Test", "pais": "AR",
                            "estado": "ACTIVO"}),
        ("ACTOR_GUARDADO", {"id_actor": base + 1, "tipo": "OPERADOR", "razon_social": "Operador Test",
                            "pais": "AR", "estado": "ACTIVO"}),
        ("ENVIO_CREADO", {"id_envio": base, "id_cliente": base, "estado": "CREADO", "prioridad": "MEDIA",
                          "origen": "TSTA1", "destino": "TSTB1", "fecha_creacion": ahora, "contenedores": []}),
    ]
    for i, (desde, hasta) in enumerate([("TSTA1", "TSTB1"), ("TSTB1", "TSTC1")], 1):
        eventos.append(("TRAMO_AGREGADO", {
            "id_tramo": base + i, "id_envio": base, "orden": i, "medio": "TERRESTRE", "codigo_origen": desde,
            "codigo_destino": hasta, "id_operador": base + 1, "id_vehiculo": None, "costo": 100.0,
            "fecha_salida": ahora, "fecha_llegada_estimada": ahora}))
    eventos.append(("TRAMO_LLEGADA", {"id_tramo": base + 1, "demora_h": 2.0, "fecha": ahora}))
    try:
        for _ in range(2):                                   # dos veces: tiene que dar lo mismo
            for tipo, payload in eventos:
                grafo.aplicar_evento(tipo, payload)
        ruta = grafo.ruta_envio(base)
        assert [(f["desde"], f["hasta"]) for f in ruta] == [("TSTA1", "TSTB1"), ("TSTB1", "TSTC1")]
        with cx.neo4j.session() as s:
            r = s.run("MATCH (:Ubicacion {codigo:'TSTA1'})-[r:RUTA]->(:Ubicacion {codigo:'TSTB1'}) "
                      "RETURN r.envios AS envios, r.llegadas AS llegadas, r.demora_prom_h AS demora").single()
            siguiente = s.run("MATCH (:Tramo {id_tramo:$a})-[:SIGUIENTE]->(:Tramo {id_tramo:$b}) RETURN count(*) AS n",
                              a=base + 1, b=base + 2).single()["n"]
        assert (r["envios"], r["llegadas"], r["demora"]) == (1, 1, 2.0)
        assert siguiente == 1
    finally:
        with cx.neo4j.session() as s:
            s.run("""MATCH (n) WHERE (n:Envio AND n.id_envio = $b) OR (n:Tramo AND n.id_tramo IN [$b + 1, $b + 2])
                        OR ((n:Cliente OR n:Operador) AND n.id_actor IN [$b, $b + 1])
                        OR (n:Ubicacion AND n.codigo IN ['TSTA1', 'TSTB1', 'TSTC1'])
                     DETACH DELETE n""", b=base).consume()
