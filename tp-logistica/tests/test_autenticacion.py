"""Pruebas del servicio de autenticación (sin bases de datos, con repositorios en memoria)."""
import pytest

from app.excepciones import ErrorAplicacion, ErrorAutenticacion, ErrorPermiso, ErrorValidacion
from app.servicios import seguridad
from app.servicios.autenticacion import MENSAJE_CREDENCIALES, ServicioAutenticacion
from tests.fakes import HistorialEnMemoria, SesionesEnMemoria, UsuariosEnMemoria, usuario_de_prueba

CLAVE = "Secreta123"
HASH = seguridad.hashear(CLAVE)


@pytest.fixture
def entorno():
    usuarios, historial, sesiones = UsuariosEnMemoria(), HistorialEnMemoria(), SesionesEnMemoria()
    usuarios.agregar(usuario_de_prueba(), HASH)
    usuarios.agregar(usuario_de_prueba(email="bloqueado@demo.com", estado="BLOQUEADO"), HASH)
    servicio = ServicioAutenticacion(usuarios, historial, sesiones, ttl_segundos=1800)
    return servicio, historial, sesiones


def test_inicio_correcto_registra_en_postgres_y_en_redis(entorno):
    servicio, historial, sesiones = entorno
    sesion = servicio.iniciar_sesion("ANA@demo.com", CLAVE)
    assert historial.filas[sesion.id_sesion]["estado"] == "ACTIVA"
    assert sesiones.existe(sesion.id_sesion)
    assert sesiones.ttl[sesion.id_sesion] == 1800
    assert sesion.roles == ["OPERADOR"] and sesion.nivel_permisos == 3


@pytest.mark.parametrize("email, clave", [("ana@demo.com", "otra"), ("nadie@demo.com", CLAVE)])
def test_credenciales_invalidas_dan_el_mismo_mensaje(entorno, email, clave):
    servicio, historial, _ = entorno
    with pytest.raises(ErrorAutenticacion, match=MENSAJE_CREDENCIALES):
        servicio.iniciar_sesion(email, clave)
    assert historial.filas == {}


def test_datos_vacios_es_error_de_validacion(entorno):
    servicio, _, _ = entorno
    with pytest.raises(ErrorValidacion):
        servicio.iniciar_sesion("  ", "")


def test_usuario_bloqueado_no_puede_entrar(entorno):
    servicio, _, _ = entorno
    with pytest.raises(ErrorAutenticacion, match="bloqueado"):
        servicio.iniciar_sesion("bloqueado@demo.com", CLAVE)


def test_si_redis_falla_la_sesion_queda_revocada_en_postgres():
    usuarios, historial = UsuariosEnMemoria(), HistorialEnMemoria()
    usuarios.agregar(usuario_de_prueba(), HASH)
    servicio = ServicioAutenticacion(usuarios, historial, SesionesEnMemoria(fallar_al_guardar=True), 1800)
    with pytest.raises(ErrorAplicacion):
        servicio.iniciar_sesion("ana@demo.com", CLAVE)
    (fila,) = historial.filas.values()
    assert fila["estado"] == "REVOCADA"          # acción de compensación


def test_validar_renueva_el_ttl(entorno):
    servicio, _, sesiones = entorno
    sesion = servicio.iniciar_sesion("ana@demo.com", CLAVE)
    sesiones.ttl[sesion.id_sesion] = 5
    servicio.validar(sesion.id_sesion)
    assert sesiones.ttl[sesion.id_sesion] == 1800


def test_sesion_vencida_queda_expirada_en_postgres(entorno):
    servicio, historial, sesiones = entorno
    sesion = servicio.iniciar_sesion("ana@demo.com", CLAVE)
    sesiones.vencer(sesion.id_sesion)
    with pytest.raises(ErrorAutenticacion, match="venció"):
        servicio.validar(sesion.id_sesion)
    assert historial.filas[sesion.id_sesion]["estado"] == "EXPIRADA"


def test_cerrar_sesion(entorno):
    servicio, historial, sesiones = entorno
    sesion = servicio.iniciar_sesion("ana@demo.com", CLAVE)
    servicio.cerrar_sesion(sesion)
    assert not sesiones.existe(sesion.id_sesion)
    assert historial.filas[sesion.id_sesion]["estado"] == "CERRADA"


def test_reconciliacion_cierra_sesiones_huerfanas(entorno):
    servicio, historial, sesiones = entorno
    viva = servicio.iniciar_sesion("ana@demo.com", CLAVE)
    huerfana = servicio.iniciar_sesion("ana@demo.com", CLAVE)
    sesiones.vencer(huerfana.id_sesion)                 # Redis la borró y nadie lo registró
    assert servicio.reconciliar_sesiones() == 1
    assert historial.filas[huerfana.id_sesion]["estado"] == "EXPIRADA"
    assert historial.filas[viva.id_sesion]["estado"] == "ACTIVA"


def test_exigir_nivel(entorno):
    servicio, _, _ = entorno
    sesion = servicio.iniciar_sesion("ana@demo.com", CLAVE)
    servicio.exigir_nivel(sesion, 3)
    with pytest.raises(ErrorPermiso):
        servicio.exigir_nivel(sesion, 5)
