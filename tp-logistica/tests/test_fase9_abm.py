"""Fase 9 (primera parte): ABM de roles, sesiones activas, envíos, contenedores de un envío y reglas de alerta."""
import re
from datetime import timedelta

import pytest

from app.dominio.modelos import ReglaAlerta, Rol, Sensor
from app.excepciones import EntidadNoEncontrada, ErrorConflicto, ErrorValidacion
from app.repositorios.neo4j.grafo import RepositorioGrafo
from app.servicios.autenticacion import ServicioAutenticacion
from app.servicios.telemetria import REGLAS_DE_FABRICA, ServicioTelemetria
from app.servicios.usuarios import ServicioUsuarios
from tests.fakes import (AHORA, CassandraEnMemoria, HistorialEnMemoria, SensoresEnMemoria, SesionesEnMemoria,
                         TelemetriaRedisEnMemoria, UsuariosEnMemoria, usuario_de_prueba)
from tests.test_envios import crear, entorno  # noqa: F401  (fixture)
from tests.test_telemetria import AlertasEnMemoria


# ---------------------------------------------------------------- roles
class RolesEnMemoria:
    def __init__(self):
        self.roles = {n: Rol(i, n, nivel) for i, (n, nivel) in enumerate(
            [("ADMINISTRADOR", 5), ("OPERADOR", 3), ("CLIENTE", 1)], 1)}
        self.asignados = {"OPERADOR": [7, 8]}

    def listar_roles(self):
        return list(self.roles.values())

    def crear_rol(self, nombre, nivel, descripcion):
        if nombre in self.roles:
            raise ErrorConflicto("ya existe")
        self.roles[nombre] = Rol(len(self.roles) + 1, nombre, nivel, descripcion)
        return self.roles[nombre].id_rol

    def modificar_rol(self, nombre, nivel, descripcion):
        self.roles[nombre] = Rol(self.roles[nombre].id_rol, nombre, nivel, descripcion)   # Rol es inmutable
        return True

    def usuarios_con_rol(self, nombre):
        return self.asignados.get(nombre, [])

    def eliminar_rol(self, nombre):
        return self.roles.pop(nombre, None) is not None


class AuthFalso:
    def __init__(self):
        self.revocados = []

    def revocar_sesiones_de_usuario(self, id_usuario):
        self.revocados.append(id_usuario)
        return 1


@pytest.fixture
def roles():
    repo, auth = RolesEnMemoria(), AuthFalso()
    return ServicioUsuarios(repo, None, auth), repo, auth


def test_alta_de_rol_valida_nombre_y_nivel(roles):
    srv, repo, _ = roles
    srv.crear_rol("auditor", 2, "Revisa reportes")
    assert repo.roles["AUDITOR"].nivel_permisos == 2                       # se guarda en mayúsculas
    for nombre, nivel in (("A", 2), ("AUDITOR 2", 2), ("NUEVO", 6)):
        with pytest.raises(ErrorValidacion):
            srv.crear_rol(nombre, nivel, "")


def test_cambiar_el_nivel_cierra_las_sesiones_de_quienes_tienen_el_rol(roles):
    srv, repo, auth = roles
    assert srv.modificar_rol("operador", 4, "Operación y reportes") == 2
    assert auth.revocados == [7, 8] and repo.roles["OPERADOR"].nivel_permisos == 4
    assert srv.modificar_rol("OPERADOR", 4, "Solo cambia el texto") == 0      # mismo nivel: no cierra nada
    with pytest.raises(ErrorConflicto):
        srv.modificar_rol("ADMINISTRADOR", 4, "")                           # nadie podría administrar


def test_baja_de_rol(roles):
    srv, repo, _ = roles
    with pytest.raises(ErrorConflicto, match="del sistema"):
        srv.eliminar_rol("CLIENTE")
    srv.crear_rol("AUDITOR", 2, "")
    repo.asignados["AUDITOR"] = [9]
    with pytest.raises(ErrorConflicto, match="1 usuarios"):
        srv.eliminar_rol("AUDITOR")
    repo.asignados["AUDITOR"] = []
    srv.eliminar_rol("auditor")
    assert "AUDITOR" not in repo.roles
    with pytest.raises(EntidadNoEncontrada):
        srv.eliminar_rol("NO_EXISTE")


# ---------------------------------------------------------------- sesiones activas
def test_listar_y_revocar_sesiones_ajenas():
    usuarios, historial, sesiones = UsuariosEnMemoria(), HistorialEnMemoria(), SesionesEnMemoria()
    from app.servicios import seguridad
    usuarios.agregar(usuario_de_prueba(), seguridad.hashear("Clave1234"))
    auth = ServicioAutenticacion(usuarios, historial, sesiones, ttl_segundos=1800, reloj=lambda: AHORA)
    s = auth.iniciar_sesion("ana@demo.com", "Clave1234")
    otra = auth.iniciar_sesion("ana@demo.com", "Clave1234")
    sesiones.vencer(otra.id_sesion)                                        # Redis ya la borró
    filas = {f["id_sesion"]: f["segundos_restantes"] for f in auth.sesiones_activas()}
    assert filas == {s.id_sesion: 1800, otra.id_sesion: -2}
    auth.revocar_sesion(s.id_sesion)
    assert not sesiones.existe(s.id_sesion) and historial.filas[s.id_sesion]["estado"] == "REVOCADA"
    with pytest.raises(EntidadNoEncontrada):
        auth.revocar_sesion(s.id_sesion)


# ---------------------------------------------------------------- envíos y sus contenedores
def test_modificar_envio(entorno):  # noqa: F811
    srv = entorno["srv"]
    e = crear(entorno)
    srv.modificar_envio(e, prioridad="URGENTE", fecha_entrega_estimada=AHORA + timedelta(days=40))
    envio = srv.obtener(e)
    assert (envio.prioridad, envio.fecha_entrega_estimada) == ("URGENTE", AHORA + timedelta(days=40))
    with pytest.raises(ErrorValidacion):
        srv.modificar_envio(e, prioridad="URGENTE")                         # no cambia nada
    with pytest.raises(ErrorValidacion):
        srv.modificar_envio(e, fecha_entrega_estimada=AHORA - timedelta(days=1))
    srv.cambiar_estado(e, "CANCELADO")
    with pytest.raises(ErrorConflicto):
        srv.modificar_envio(e, prioridad="BAJA")


def test_agregar_y_quitar_contenedores_antes_de_salir(entorno):  # noqa: F811
    srv, conts = entorno["srv"], entorno["contenedores"]
    e = crear(entorno, contenedores=(1,), volumen=20)
    with pytest.raises(ErrorConflicto, match="al menos un contenedor"):
        srv.quitar_contenedor(e, 1)
    srv.agregar_contenedor(e, 2)
    assert conts.contenedor(2).estado == "EN_USO"
    with pytest.raises(ErrorConflicto):
        srv.agregar_contenedor(e, 2)                                        # ya está
    srv.quitar_contenedor(e, 1)
    assert conts.contenedor(1).estado == "DISPONIBLE" and srv.obtener(e).contenedores == [2]
    srv.agregar_tramo(e, "TERRESTRE", "ARBUE", entorno["camionero"], AHORA + timedelta(hours=1),
                      AHORA + timedelta(hours=5))
    srv.cambiar_estado(e, "EN_TRANSITO")
    with pytest.raises(ErrorConflicto, match="antes de que salga"):
        srv.agregar_contenedor(e, 1)


def test_quitar_un_contenedor_no_puede_dejar_la_carga_sin_lugar(entorno):  # noqa: F811
    srv = entorno["srv"]
    e = crear(entorno, contenedores=(1, 2), volumen=50)                     # 30 + 30 m³
    with pytest.raises(ErrorConflicto, match="no entra la carga"):
        srv.quitar_contenedor(e, 1)


def test_los_eventos_nuevos_traen_todos_los_parametros_de_su_cypher():
    payloads = {"ENVIO_MODIFICADO": {"id_envio": 1, "prioridad": "ALTA", "fecha_entrega_estimada": "x", "fecha": "x"},
                "CONTENEDOR_ASIGNADO": {"id_envio": 1, "id_contenedor": 2, "desde": "x"},
                "CONTENEDOR_QUITADO": {"id_envio": 1, "id_contenedor": 2}}
    for tipo, payload in payloads.items():
        cypher = " ".join(RepositorioGrafo.sentencias_de(tipo, payload))
        assert set(re.findall(r"\$(\w+)", cypher)) <= set(payload), tipo


# ---------------------------------------------------------------- reglas de alerta
class ReglasEnMemoria:
    def __init__(self):
        self.reglas = {k: ReglaAlerta(r.tipo, r.umbral, r.unidad, r.activa, r.descripcion)
                       for k, r in REGLAS_DE_FABRICA.items()}

    def listar(self):
        return list(self.reglas.values())

    def actualizar(self, tipo, umbral, activa):
        self.reglas[tipo].umbral, self.reglas[tipo].activa = umbral, activa
        return True


def telemetria(reglas):
    sensores = SensoresEnMemoria([Sensor(1, "MULTI", "Acme", temp_min=2, temp_max=8, id_contenedor=10)],
                                 envios_por_contenedor={10: 500})
    c, m = CassandraEnMemoria(), AlertasEnMemoria()
    return ServicioTelemetria(sensores, c, TelemetriaRedisEnMemoria(), m, reloj=lambda: AHORA,
                              repo_reglas=reglas), c, m


def test_el_umbral_de_humedad_sale_de_la_regla():
    reglas = ReglasEnMemoria()
    srv, _, m = telemetria(reglas)
    assert srv.registrar_lectura(1, AHORA, humedad=80) == []                # 80 < 85 de fábrica
    srv.modificar_regla("HUMEDAD", 75, True)
    assert [a.tipo for a in srv.registrar_lectura(1, AHORA, humedad=80)] == ["HUMEDAD"]
    assert m.alertas[-1].umbral == 75


def test_regla_desactivada_guarda_la_anomalia_pero_no_alerta():
    reglas = ReglasEnMemoria()
    srv, c, _ = telemetria(reglas)
    srv.modificar_regla("TEMPERATURA", None, False)
    assert srv.registrar_lectura(1, AHORA, temperatura=12) == []
    assert len(c.anomalias) == 1                                            # el dato queda para el análisis


def test_validaciones_de_las_reglas_y_sensor_inactivo():
    reglas = ReglasEnMemoria()
    srv, _, _ = telemetria(reglas)
    with pytest.raises(ErrorValidacion):
        srv.modificar_regla("BATERIA_BAJA", 150, True)
    with pytest.raises(ErrorValidacion):
        srv.modificar_regla("TEMPERATURA", 10, True)                        # la temperatura usa el rango del sensor
    with pytest.raises(EntidadNoEncontrada):
        srv.modificar_regla("INVENTADA", 1, True)
    srv.registrar_lectura(1, AHORA - timedelta(minutes=20), bateria=90)
    assert srv.alertar_sin_contacto() == []                                 # 20 min < 30 de la regla
    srv.modificar_regla("SENSOR_INACTIVO", 10, True)
    assert len(srv.alertar_sin_contacto()) == 1
    srv.modificar_regla("SENSOR_INACTIVO", 10, False)
    assert srv.alertar_sin_contacto() == []                                 # regla apagada
