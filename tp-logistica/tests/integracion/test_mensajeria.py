"""Integración de la fase 5 (mensajería).

- test_redis_*: solo Redis (contadores, limitador, presencia).
- test_conversacion_*: MongoDB + Redis + PostgreSQL con usuarios de prueba; borra lo que crea.
"""
import random
import uuid
from datetime import datetime, timezone

import pytest

from app.config import Config
from app.dominio.modelos import Sesion
from app.infra.conexiones import GestorConexiones
from app.repositorios.mongo.mensajeria import RepositorioConversaciones, RepositorioGrupos, RepositorioMensajes
from app.repositorios.postgres.usuarios import RepositorioUsuarios
from app.repositorios.redis import mensajeria as redis_mensajeria
from app.servicios import seguridad
from app.servicios.mensajeria import ServicioMensajeria

pytestmark = pytest.mark.integracion


@pytest.fixture(scope="module")
def cx():
    gestor = GestorConexiones(Config.desde_entorno())
    try:
        gestor.redis.ping()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Redis no disponible: {exc}")
    yield gestor
    gestor.cerrar()


def test_redis_contadores_limitador_y_presencia(cx):
    r = redis_mensajeria.RepositorioMensajeriaRedis(cx)
    u = 900_000 + random.randint(0, 99_999)
    claves = [f"noleidos:{u}", f"limite:mensajes:{u}", f"usuario:{u}:sesiones", f"sesion:test-{u}"]
    try:
        assert r.no_leidos(u) is None                       # nunca calculado
        r.reemplazar_no_leidos(u, {})
        assert r.no_leidos(u) == {}                         # calculado y sin pendientes (marca "_")
        r.sumar_no_leidos([u], "conv1")
        r.sumar_no_leidos([u], "conv1")
        assert r.no_leidos(u) == {"conv1": 2}
        r.limpiar(u, "conv1")
        assert r.no_leidos(u) == {}
        assert all(r.permitir_envio(u) for _ in range(redis_mensajeria.MENSAJES_POR_MINUTO))
        assert r.permitir_envio(u) is False                 # el mensaje 21 del minuto
        assert 0 < cx.redis.ttl(f"limite:mensajes:{u}") <= 60
        assert r.en_linea([u]) == set()
        cx.redis.sadd(f"usuario:{u}:sesiones", f"test-{u}")
        assert r.en_linea([u]) == set()                     # la sesión ya venció (no existe la clave)
        cx.redis.hset(f"sesion:test-{u}", mapping={"id_usuario": u})
        assert r.en_linea([u]) == {u}
    finally:
        cx.redis.delete(*claves)


@pytest.fixture
def usuarios(cx):
    try:
        cx.mongo.client.admin.command("ping")
        with cx.transaccion() as cur:
            cur.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Faltan MongoDB o PostgreSQL: {exc}")
    repo = RepositorioUsuarios(cx)
    repo.asegurar_rol("OPERADOR", 3, "Operación diaria")
    ids = [repo.crear("Prueba", f"Chat{n}", f"chat-{uuid.uuid4().hex[:8]}@test.local",
                      seguridad.hashear("Clave123"), ["OPERADOR"]) for n in range(3)]
    yield ids
    convs = [c["_id"] for c in cx.mongo["conversaciones"].find({"participantes": {"$in": ids}})]
    cx.mongo["mensajes"].delete_many({"id_conversacion": {"$in": convs}})
    cx.mongo["conversaciones"].delete_many({"_id": {"$in": convs}})
    cx.mongo["grupos_trabajo"].delete_many({"creado_por": {"$in": ids}})
    cx.redis.delete(*[f"noleidos:{i}" for i in ids], *[f"limite:mensajes:{i}" for i in ids])
    with cx.transaccion() as cur:
        cur.execute("DELETE FROM usuario WHERE id_usuario = ANY(%s)", (ids,))


def test_conversacion_privada_y_grupo_reales(cx, usuarios):
    a, b, c = usuarios
    ahora = datetime.now(timezone.utc)
    s = {i: Sesion(f"s{i}", i, f"{i}@t", f"U{i}", ["OPERADOR"], 3, ahora, ahora) for i in usuarios}
    srv = ServicioMensajeria(RepositorioUsuarios(cx), RepositorioGrupos(cx), RepositorioConversaciones(cx),
                             RepositorioMensajes(cx), redis_mensajeria.RepositorioMensajeriaRedis(cx))
    srv.enviar_privado(s[a], b, "hola")
    srv.enviar_privado(s[b], a, "hola, ¿qué tal?")
    assert srv.conversacion_privada(s[a], b).id_conversacion == srv.conversacion_privada(s[b], a).id_conversacion
    assert srv.total_no_leidos(s[a]) == 1
    cx.redis.delete(f"noleidos:{a}")                        # Redis "pierde" el contador...
    assert srv.total_no_leidos(s[a]) == 1                   # ...y se recalcula desde MongoDB
    id_conv = srv.bandeja(s[a])[0]["conversacion"].id_conversacion
    assert [m.contenido for m in srv.abrir(id_conv, s[a])] == ["hola", "hola, ¿qué tal?"]
    assert srv.total_no_leidos(s[a]) == 0

    nombre = f"Grupo prueba {uuid.uuid4().hex[:6]}"
    id_grupo = srv.crear_grupo(s[a], nombre, "integración", [b])
    srv.agregar_integrante(id_grupo, c, s[a])
    grupo = cx.mongo["grupos_trabajo"].find_one({"nombre": nombre})
    assert [i["rol_en_grupo"] for i in grupo["integrantes"]] == ["ADMIN", "MIEMBRO", "MIEMBRO"]
    conv = cx.mongo["conversaciones"].find_one({"id_grupo": grupo["_id"]})
    assert sorted(conv["participantes"]) == sorted([a, b, c])
    assert srv.total_no_leidos(s[c]) >= 1                   # el aviso "agregó a ..."
