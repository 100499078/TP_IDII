"""Alta, modificación y baja lógica de actores."""
import pytest

from app.dominio.modelos import Actor
from app.excepciones import ErrorConflicto, ErrorValidacion
from app.servicios.actores import ServicioActores
from tests.fakes import ActoresEnMemoria


@pytest.fixture
def srv():
    return ServicioActores(ActoresEnMemoria())


def cliente(**cambios):
    datos = dict(id_actor=None, tipo="CLIENTE", razon_social="Cliente SA", cuit="30-71111111-1",
                 email="C@C.COM ", pais="ar")
    datos.update(cambios)
    return Actor(**datos)


def test_alta_normaliza_email_y_pais(srv):
    a = srv.obtener(srv.crear(cliente()))
    assert (a.email, a.pais, a.estado) == ("c@c.com", "AR", "ACTIVO")


def test_cuit_invalido(srv):
    with pytest.raises(ErrorValidacion, match="verificador"):
        srv.crear(cliente(cuit="30-71111111-9"))


def test_operador_sin_medios(srv):
    with pytest.raises(ErrorValidacion, match="medio"):
        srv.crear(Actor(None, "OPERADOR", "Op SA", "30-71333333-2", "o@o.com", "AR"))


def test_modificar_no_cambia_el_cuit(srv):
    id_actor = srv.crear(cliente())
    a = srv.obtener(id_actor)
    a.cuit, a.razon_social = "20-12345678-6", "Nuevo Nombre SA"
    srv.modificar(a)
    guardado = srv.obtener(id_actor)
    assert (guardado.cuit, guardado.razon_social) == ("30-71111111-1", "Nuevo Nombre SA")


def test_baja_logica_y_bloqueo_con_envios_en_curso():
    repo = ActoresEnMemoria()
    srv = ServicioActores(repo)
    id_actor = srv.crear(cliente())
    repo.envios_en_curso[id_actor] = 1
    with pytest.raises(ErrorConflicto, match="envíos en curso"):
        srv.dar_de_baja(id_actor)
    repo.envios_en_curso[id_actor] = 0
    srv.dar_de_baja(id_actor)
    assert srv.obtener(id_actor).estado == "INACTIVO"      # sigue existiendo: baja lógica
