"""Pruebas de integración contra PostgreSQL y Redis reales (docker compose up).

Si los motores no responden, las pruebas se saltean en lugar de fallar.
"""
import uuid

import pytest

from app.config import Config
from app.excepciones import ErrorConexion
from app.infra.conexiones import GestorConexiones
from app.repositorios.postgres.sesiones import RepositorioHistorialSesiones
from app.repositorios.postgres.usuarios import RepositorioUsuarios
from app.repositorios.redis.sesiones import RepositorioSesionesActivas
from app.servicios import seguridad
from app.servicios.autenticacion import ServicioAutenticacion

pytestmark = pytest.mark.integracion


@pytest.fixture(scope="module")
def cx():
    gestor = GestorConexiones(Config.desde_entorno())
    try:
        gestor.redis.ping()
        with gestor.transaccion() as cur:
            cur.execute("SELECT 1 FROM rol LIMIT 1")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL o Redis no disponibles: {exc}")
    yield gestor
    gestor.cerrar()


@pytest.fixture
def usuario(cx):
    repo = RepositorioUsuarios(cx)
    repo.asegurar_rol("OPERADOR", 3, "Operación diaria")
    email = f"prueba-{uuid.uuid4().hex[:8]}@test.local"
    id_usuario = repo.crear("Prueba", "Integración", email, seguridad.hashear("Clave123"), ["OPERADOR"])
    yield email
    with cx.transaccion() as cur:
        cur.execute("DELETE FROM sesion_historial WHERE id_usuario = %s", (id_usuario,))
        cur.execute("DELETE FROM usuario WHERE id_usuario = %s", (id_usuario,))


def test_ciclo_completo_de_sesion(cx, usuario):
    historial = RepositorioHistorialSesiones(cx)
    servicio = ServicioAutenticacion(RepositorioUsuarios(cx), historial, RepositorioSesionesActivas(cx), 60)
    sesion = servicio.iniciar_sesion(usuario, "Clave123")

    assert 0 < cx.redis.ttl(f"sesion:{sesion.id_sesion}") <= 60
    assert sesion.id_sesion in cx.redis.smembers(f"usuario:{sesion.id_usuario}:sesiones")
    with cx.transaccion() as cur:
        cur.execute("SELECT estado FROM sesion_historial WHERE id_sesion = %s", (sesion.id_sesion,))
        assert cur.fetchone()["estado"] == "ACTIVA"

    servicio.cerrar_sesion(sesion)
    assert not cx.redis.exists(f"sesion:{sesion.id_sesion}")
    with cx.transaccion() as cur:
        cur.execute("SELECT estado, fecha_fin FROM sesion_historial WHERE id_sesion = %s", (sesion.id_sesion,))
        fila = cur.fetchone()
    assert fila["estado"] == "CERRADA" and fila["fecha_fin"] is not None


def test_la_transaccion_hace_rollback_si_algo_falla(cx):
    nombre = f"ROL_{uuid.uuid4().hex[:6]}"
    with pytest.raises(RuntimeError):
        with cx.transaccion() as cur:
            cur.execute("INSERT INTO rol (nombre, nivel_permisos) VALUES (%s, 1)", (nombre,))
            raise RuntimeError("falla a mitad de la transacción")
    with cx.transaccion() as cur:
        cur.execute("SELECT count(*) AS n FROM rol WHERE nombre = %s", (nombre,))
        assert cur.fetchone()["n"] == 0


def test_motor_caido_da_error_de_conexion():
    cfg = Config.desde_entorno()
    caido = GestorConexiones(Config(**{**cfg.__dict__, "pg_port": 1}))
    with pytest.raises(ErrorConexion, match="PostgreSQL"):
        with caido.transaccion() as cur:
            cur.execute("SELECT 1")
