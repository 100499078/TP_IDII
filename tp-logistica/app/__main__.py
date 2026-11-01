"""Punto de entrada:  python -m app

Acá se arma la aplicación ("raíz de composición"): se crean las conexiones,
los repositorios y los servicios, y se le pasan a cada uno sus dependencias.
Ninguna clase crea por su cuenta lo que necesita; lo recibe en el constructor
(inyección de dependencias). Por eso en las pruebas se pueden reemplazar los
repositorios reales por versiones en memoria.
"""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.config import RAIZ_PROYECTO, Config
from app.infra.conexiones import GestorConexiones
from app.logs import configurar_logging
from app.presentacion.consola import AplicacionConsola, ServiciosOperativos
from app.procesos.sincronizador import HiloSincronizacion
from app.repositorios.cassandra.telemetria import RepositorioTelemetria
from app.repositorios.mongo.alertas import RepositorioAlertas
from app.repositorios.mongo.eventos import RepositorioEventos
from app.repositorios.mongo.incidentes import RepositorioIncidentes
from app.repositorios.mongo.mensajeria import RepositorioConversaciones, RepositorioGrupos, RepositorioMensajes
from app.repositorios.mongo.predicciones import RepositorioPredicciones
from app.repositorios.mongo.reportes import RepositorioReportes
from app.repositorios.neo4j.grafo import RepositorioGrafo
from app.repositorios.postgres.actores import RepositorioActores
from app.repositorios.postgres.catalogos import RepositorioCatalogos
from app.repositorios.postgres.cierres import RepositorioCierres
from app.repositorios.postgres.comercial import RepositorioComercial
from app.repositorios.postgres.controles import RepositorioControles
from app.repositorios.postgres.contenedores import RepositorioContenedores
from app.repositorios.postgres.envios import RepositorioEnvios
from app.repositorios.postgres.outbox import RepositorioOutbox
from app.repositorios.postgres.reglas import RepositorioReglas
from app.repositorios.postgres.reportes import RepositorioReportesSQL
from app.repositorios.postgres.sesiones import RepositorioHistorialSesiones
from app.repositorios.postgres.usuarios import RepositorioUsuarios
from app.repositorios.redis.cache import RepositorioCache
from app.repositorios.redis.cola_sync import GRUPO_TRAZABILIDAD, RepositorioColaSync
from app.repositorios.redis.locks import RepositorioLocks
from app.repositorios.redis.mensajeria import RepositorioMensajeriaRedis
from app.repositorios.redis.procesos import RepositorioEstadoProcesos
from app.repositorios.redis.sesiones import RepositorioSesionesActivas
from app.repositorios.redis.telemetria import RepositorioTelemetriaRedis
from app.servicios.actores import ServicioActores
from app.servicios.autenticacion import ServicioAutenticacion
from app.servicios.catalogos import ServicioCatalogos
from app.servicios.comercial import ServicioComercial
from app.servicios.contenedores import ServicioContenedores
from app.servicios.envios import ServicioEnvios
from app.servicios.estado import ServicioEstado
from app.servicios.mensajeria import ServicioMensajeria
from app.servicios.procesos import Proceso, ServicioProcesos, mes_anterior
from app.servicios.reportes import ServicioReportes
from app.servicios.riesgo import ServicioRiesgo
from app.servicios.simulacion import SimuladorSensores
from app.servicios.sincronizacion import Proyeccion, ServicioSincronizacion
from app.servicios.telemetria import ServicioTelemetria
from app.servicios.trazabilidad import ProyeccionTrazabilidad, ServicioTrazabilidad
from app.servicios.usuarios import ServicioUsuarios
from app.servicios.verificacion import ServicioVerificacion


def construir_sincronizacion(conexiones: GestorConexiones) -> ServicioSincronizacion:
    """El servicio de sincronización con todos sus repositorios (lo usan la consola,
    el hilo de fondo y herramientas/sincronizar.py)."""
    repo_envios, repo_catalogos = RepositorioEnvios(conexiones), RepositorioCatalogos(conexiones)
    # Fase 4: el mismo stream alimenta también la línea de tiempo en MongoDB, con otro grupo
    trazabilidad = Proyeccion("MongoDB (trazabilidad)", RepositorioColaSync(conexiones, GRUPO_TRAZABILIDAD),
                              ProyeccionTrazabilidad(RepositorioEventos(conexiones), repo_envios, repo_catalogos))
    return ServicioSincronizacion(RepositorioOutbox(conexiones), RepositorioColaSync(conexiones),
                                  RepositorioGrafo(conexiones), RepositorioActores(conexiones),
                                  repo_catalogos, RepositorioContenedores(conexiones), repo_envios,
                                  otras=[trazabilidad], repo_incidentes=RepositorioIncidentes(conexiones))


def construir_telemetria(conexiones: GestorConexiones) -> tuple[ServicioTelemetria, SimuladorSensores]:
    """Telemetría IoT (Cassandra + Redis + MongoDB) y su simulador (los usa también
    herramientas/simulador_sensores.py)."""
    repo_contenedores = RepositorioContenedores(conexiones)
    telemetria = ServicioTelemetria(repo_contenedores, RepositorioTelemetria(conexiones),
                                    RepositorioTelemetriaRedis(conexiones), RepositorioAlertas(conexiones),
                                    repo_reglas=RepositorioReglas(conexiones))      # fase 9
    simulador = SimuladorSensores(telemetria, repo_contenedores, RepositorioEnvios(conexiones),
                                  RepositorioCatalogos(conexiones))
    return telemetria, simulador


def construir_verificacion(conexiones: GestorConexiones) -> ServicioVerificacion:
    """Fase 8: el verificador de consistencia entre motores (lo usan la consola y
    herramientas/verificar.py)."""
    colas = [("Neo4j", RepositorioColaSync(conexiones)),
             ("MongoDB", RepositorioColaSync(conexiones, GRUPO_TRAZABILIDAD))]
    return ServicioVerificacion(RepositorioControles(conexiones), RepositorioOutbox(conexiones), colas,
                                RepositorioGrafo(conexiones), RepositorioEventos(conexiones),
                                RepositorioIncidentes(conexiones), RepositorioTelemetria(conexiones),
                                RepositorioTelemetriaRedis(conexiones), RepositorioContenedores(conexiones),
                                RepositorioHistorialSesiones(conexiones), RepositorioSesionesActivas(conexiones),
                                repo_reportes=RepositorioReportes(conexiones))               # fase 9


@dataclass
class Fase7:
    procesos: ServicioProcesos
    reportes: ServicioReportes
    riesgo: ServicioRiesgo
    cierres: RepositorioCierres


def construir_procesos(conexiones: GestorConexiones, config: Config) -> Fase7:
    """Los procesos periódicos y los reportes (los usan la consola y herramientas/planificador.py)."""
    repo_actores, repo_incidentes = RepositorioActores(conexiones), RepositorioIncidentes(conexiones)
    repo_grafo, repo_sql = RepositorioGrafo(conexiones), RepositorioReportesSQL(conexiones)
    cierres, locks = RepositorioCierres(conexiones), RepositorioLocks(conexiones)
    comercial = ServicioComercial(RepositorioComercial(conexiones), repo_actores, locks,
                                  repo_reportes=RepositorioReportes(conexiones))           # fase 9
    telemetria, _ = construir_telemetria(conexiones)
    autenticacion = ServicioAutenticacion(RepositorioUsuarios(conexiones), RepositorioHistorialSesiones(conexiones),
                                          RepositorioSesionesActivas(conexiones), config.sesion_ttl_segundos)
    riesgo = ServicioRiesgo(repo_sql, repo_grafo, repo_incidentes, RepositorioPredicciones(conexiones))
    reportes = ServicioReportes(RepositorioReportes(conexiones), repo_sql, repo_incidentes, repo_grafo,
                                RAIZ_PROYECTO / "reportes", repo_predicciones=RepositorioPredicciones(conexiones))

    def cierre() -> str:
        periodo, desde, hasta = mes_anterior(datetime.now(timezone.utc))
        n = cierres.cerrar_periodo(periodo, desde, hasta)
        return f"{periodo}: {n} cuentas cerradas" if n else f"{periodo}: ya estaba cerrado"

    procesos = [
        Proceso("reportes", "Genera los reportes pedidos (cola en MongoDB)", timedelta(seconds=10),
                reportes.procesar_pendientes),
        Proceso("sensores", "Alerta sensores sin contacto (umbral de la regla SENSOR_INACTIVO)", timedelta(minutes=5),
                lambda: f"{len(telemetria.alertar_sin_contacto())} alertas SENSOR_INACTIVO"),
        Proceso("sesiones", "Cierra en PostgreSQL las sesiones vencidas en Redis", timedelta(minutes=10),
                lambda: f"{autenticacion.reconciliar_sesiones()} sesiones marcadas EXPIRADA"),
        Proceso("riesgo", "Predicción de riesgo de demora de los envíos en curso", timedelta(hours=1),
                riesgo.calcular_todos),
        # Fase 8: la facturación automática que pide el enunciado (antes solo estaba en el menú 10)
        Proceso("facturacion", "Factura los envíos entregados de todos los clientes", timedelta(days=1),
                lambda: f"{len(comercial.facturar_todos())} facturas emitidas"),
        Proceso("vencidas", "Marca las facturas vencidas", timedelta(days=1),
                lambda: f"{comercial.marcar_vencidas()} facturas pasaron a VENCIDA"),
        Proceso("cierre", "Cierre mensual de las cuentas corrientes (mes anterior)", timedelta(days=1), cierre),
    ]
    return Fase7(ServicioProcesos(procesos, RepositorioEstadoProcesos(conexiones), locks), reportes, riesgo, cierres)


def construir_aplicacion(config: Config, con_sincronizador: bool = True
                         ) -> tuple[AplicacionConsola, GestorConexiones, HiloSincronizacion | None]:
    conexiones = GestorConexiones(config)

    # Repositorios (capa de datos)
    repo_usuarios = RepositorioUsuarios(conexiones)
    repo_historial = RepositorioHistorialSesiones(conexiones)
    repo_sesiones = RepositorioSesionesActivas(conexiones)
    repo_actores = RepositorioActores(conexiones)
    repo_catalogos = RepositorioCatalogos(conexiones)
    repo_contenedores = RepositorioContenedores(conexiones)
    repo_envios = RepositorioEnvios(conexiones)
    repo_grafo = RepositorioGrafo(conexiones)

    # Servicios (capa de negocio)
    autenticacion = ServicioAutenticacion(repo_usuarios, repo_historial, repo_sesiones,
                                          ttl_segundos=config.sesion_ttl_segundos)
    estado = ServicioEstado(conexiones)

    # Proceso de fondo: PostgreSQL (outbox) → Redis (stream) → Neo4j, con conexiones propias
    hilo = None
    if con_sincronizador:
        def fabrica():
            propias = GestorConexiones(config)
            return construir_sincronizacion(propias), propias
        hilo = HiloSincronizacion(fabrica)

    telemetria, simulador = construir_telemetria(conexiones)
    comercial = ServicioComercial(RepositorioComercial(conexiones), repo_actores, RepositorioLocks(conexiones),
                                  repo_reportes=RepositorioReportes(conexiones))           # fase 9
    trazabilidad = ServicioTrazabilidad(repo_envios, repo_catalogos, RepositorioEventos(conexiones),
                                        RepositorioIncidentes(conexiones), RepositorioCache(conexiones),
                                        RepositorioTelemetria(conexiones), repo_grafo,
                                        carpeta_mapas=RAIZ_PROYECTO / "mapas")                  # fase 9
    servicios = ServiciosOperativos(
        actores=ServicioActores(repo_actores),
        usuarios=ServicioUsuarios(repo_usuarios, repo_actores, autenticacion),
        contenedores=ServicioContenedores(repo_contenedores),
        catalogos=ServicioCatalogos(repo_catalogos, repo_actores),
        envios=ServicioEnvios(repo_envios, repo_actores, repo_catalogos, repo_contenedores, repo_grafo,
                              control_credito=comercial.controlar_credito),
        sincronizacion=construir_sincronizacion(conexiones),
        hilo_sincronizacion=hilo,
        telemetria=telemetria,
        simulador=simulador,
        trazabilidad=trazabilidad,
        comercial=comercial,
        fase7=construir_procesos(conexiones, config),
        verificacion=construir_verificacion(conexiones),
        mensajeria=ServicioMensajeria(repo_usuarios, RepositorioGrupos(conexiones), RepositorioConversaciones(conexiones),
                                      RepositorioMensajes(conexiones), RepositorioMensajeriaRedis(conexiones)),
    )

    # Presentación
    return AplicacionConsola(autenticacion, estado, servicios), conexiones, hilo


def main() -> int:
    config = Config.desde_entorno()
    configurar_logging(config.log_carpeta, config.log_nivel)
    log = logging.getLogger("app")
    log.info("Inicio de la aplicación")
    con_sync = os.getenv("SINCRONIZADOR", "1") != "0"
    app, conexiones, hilo = construir_aplicacion(config, con_sincronizador=con_sync)
    if hilo:
        hilo.start()
    try:
        app.ejecutar()
        return 0
    except KeyboardInterrupt:
        print("\nSaliendo...")
        return 0
    finally:
        if hilo:
            hilo.detener()
        conexiones.cerrar()
        log.info("Fin de la aplicación")


if __name__ == "__main__":
    sys.exit(main())
