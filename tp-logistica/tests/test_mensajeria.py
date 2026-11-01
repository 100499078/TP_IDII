"""Mensajería (fase 5): privados, grupos, no leídos en Redis y reglas de acceso."""
import pytest

from app.dominio.modelos import Rol, Sesion, Usuario
from app.excepciones import EntidadNoEncontrada, ErrorConflicto, ErrorPermiso, ErrorValidacion
from app.servicios.mensajeria import ServicioMensajeria
from tests.fakes import (AHORA, ConversacionesEnMemoria, GruposEnMemoria, MensajeriaRedisEnMemoria,
                         MensajesEnMemoria, UsuariosListaEnMemoria)


def usuario(i, nombre, id_actor=None, estado="ACTIVO"):
    return Usuario(i, nombre, "Test", f"{nombre.lower()}@x.com", estado, id_actor,
                   [Rol(1, "CLIENTE" if id_actor else "OPERADOR", 1 if id_actor else 3)])


def sesion(u, nivel=3):
    return Sesion(f"s{u.id_usuario}", u.id_usuario, u.email, u.nombre, [], nivel, AHORA, AHORA, u.id_actor)


@pytest.fixture
def m():
    ana, beto, caro = usuario(1, "Ana"), usuario(2, "Beto"), usuario(3, "Caro")
    cliente, otro_cliente = usuario(4, "Cli", id_actor=10), usuario(5, "Otro", id_actor=11)
    redis = MensajeriaRedisEnMemoria()
    srv = ServicioMensajeria(UsuariosListaEnMemoria([ana, beto, caro, cliente, otro_cliente]), GruposEnMemoria(),
                             ConversacionesEnMemoria(), MensajesEnMemoria(), redis, reloj=lambda: AHORA)
    return dict(srv=srv, redis=redis, ana=sesion(ana), beto=sesion(beto), caro=sesion(caro),
                cliente=sesion(cliente, 1))


def test_mensaje_privado_y_no_leidos(m):
    srv = m["srv"]
    srv.enviar_privado(m["ana"], 2, "Hola Beto")
    srv.enviar_privado(m["ana"], 2, "¿Llegó el contenedor?")
    assert srv.total_no_leidos(m["beto"]) == 2
    assert srv.total_no_leidos(m["ana"]) == 0                  # los propios no cuentan
    conv = srv.bandeja(m["beto"])[0]
    assert conv["conversacion"].titulo == "Ana Test" and conv["no_leidos"] == 2
    mensajes = srv.abrir(conv["conversacion"].id_conversacion, m["beto"])
    assert [x.contenido for x in mensajes] == ["Hola Beto", "¿Llegó el contenedor?"]
    assert srv.total_no_leidos(m["beto"]) == 0                 # abrir la conversación la marca leída


def test_una_sola_conversacion_privada_por_par(m):
    a = m["srv"].conversacion_privada(m["ana"], 2)
    b = m["srv"].conversacion_privada(m["beto"], 1)
    assert a.id_conversacion == b.id_conversacion


def test_si_redis_pierde_los_contadores_se_recalculan_desde_mongo(m):
    m["srv"].enviar_privado(m["ana"], 2, "uno")
    m["redis"].hashes.clear()                                  # Redis se reinició
    assert m["srv"].total_no_leidos(m["beto"]) == 1            # se contó en MongoDB
    assert m["redis"].hashes[2]                                # y se volvió a guardar en Redis


def test_sin_redis_se_puede_escribir_igual(m):
    m["redis"].caido = True
    m["srv"].enviar_privado(m["ana"], 2, "sin Redis")
    assert m["srv"].total_no_leidos(m["beto"]) == 1            # cuenta en MongoDB


def test_un_cliente_solo_escribe_a_personal_interno(m):
    srv = m["srv"]
    assert {c["usuario"].id_usuario for c in srv.contactos(m["cliente"])} == {1, 2, 3}
    with pytest.raises(ErrorPermiso):
        srv.enviar_privado(m["cliente"], 5, "hola")
    with pytest.raises(ErrorPermiso):
        srv.crear_grupo(m["cliente"], "Clientes", "", [1])


def test_no_se_lee_una_conversacion_ajena(m):
    c = m["srv"].conversacion_privada(m["ana"], 2)
    with pytest.raises(EntidadNoEncontrada):
        m["srv"].abrir(c.id_conversacion, m["caro"])


def test_limite_de_mensajes_por_minuto(m):
    m["redis"].limite = 3
    for i in range(3):
        m["srv"].enviar_privado(m["ana"], 2, f"m{i}")
    with pytest.raises(ErrorConflicto, match="demasiados"):
        m["srv"].enviar_privado(m["ana"], 2, "uno más")


def test_mensaje_vacio_o_largo(m):
    with pytest.raises(ErrorValidacion):
        m["srv"].enviar_privado(m["ana"], 2, "   ")
    with pytest.raises(ErrorValidacion):
        m["srv"].enviar_privado(m["ana"], 2, "x" * 4001)


def test_grupo_conversacion_grupal_e_integrantes(m):
    srv = m["srv"]
    id_grupo = srv.crear_grupo(m["ana"], "Operaciones Rosario", "", [2])
    conv = srv.bandeja(m["beto"])[0]["conversacion"]
    assert conv.tipo == "GRUPAL" and conv.titulo == "Grupo Operaciones Rosario"
    with pytest.raises(ErrorPermiso):
        srv.agregar_integrante(id_grupo, 3, m["beto"])         # Beto es MIEMBRO, no ADMIN
    srv.agregar_integrante(id_grupo, 3, m["ana"])
    srv.enviar(conv.id_conversacion, "Hola a todos", m["beto"])
    assert srv.total_no_leidos(m["caro"]) >= 1                 # Caro ya recibe los mensajes del grupo
    with pytest.raises(ErrorConflicto):
        srv.agregar_integrante(id_grupo, 3, m["ana"])          # ya estaba
    srv.quitar_integrante(id_grupo, 3, m["ana"])
    with pytest.raises(ErrorConflicto, match="al menos 2"):
        srv.quitar_integrante(id_grupo, 2, m["ana"])
    with pytest.raises(EntidadNoEncontrada):
        srv.abrir(conv.id_conversacion, m["caro"])             # ya no participa


def test_grupo_sin_otros_integrantes_o_nombre_repetido(m):
    with pytest.raises(ErrorValidacion):
        m["srv"].crear_grupo(m["ana"], "Solo yo", "", [1])
    m["srv"].crear_grupo(m["ana"], "Aduana", "", [2])
    with pytest.raises(ErrorConflicto):
        m["srv"].crear_grupo(m["beto"], "Aduana", "", [1])


def test_los_grupos_son_internos(m):
    with pytest.raises(ErrorPermiso, match="internos"):
        m["srv"].crear_grupo(m["ana"], "Con cliente", "", [2, 4])
    id_grupo = m["srv"].crear_grupo(m["ana"], "Internos", "", [2])
    with pytest.raises(ErrorPermiso):
        m["srv"].agregar_integrante(id_grupo, 4, m["ana"])
    assert {c["usuario"].id_usuario for c in m["srv"].contactos(m["ana"], solo_internos=True)} == {2, 3}
