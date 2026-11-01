"""Gestor de conexiones a los cinco motores.

Es el único lugar del programa que sabe CÓMO conectarse a cada base. Los
repositorios le piden la conexión ya abierta y no conocen direcciones ni
contraseñas.

Las conexiones se abren de forma perezosa (la primera vez que se usan). Si un
motor está caído, la aplicación igual arranca y solo fallan las opciones que
lo necesitan, con un mensaje claro (ErrorConexion).
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator

from app.config import Config
from app.excepciones import ErrorConexion

log = logging.getLogger(__name__)


@dataclass
class EstadoMotor:
    motor: str
    ok: bool
    detalle: str
    milisegundos: float | None = None


class GestorConexiones:
    def __init__(self, config: Config):
        self._cfg = config
        self._pg = None
        self._mongo_cliente = None
        self._neo4j = None
        self._cassandra_cluster = None
        self._cassandra_sesion = None
        self._redis = None

    # ------------------------------------------------------------------ PostgreSQL
    def _conexion_pg(self):
        import psycopg
        from psycopg.rows import dict_row

        if self._pg is None or self._pg.closed or self._pg.broken:
            try:
                # autocommit=True: cada bloque transaccional lo abrimos explícitamente
                # con transaccion(); así nunca queda una transacción colgada.
                self._pg = psycopg.connect(self._cfg.pg_dsn, autocommit=True, row_factory=dict_row)
                with self._pg.cursor() as cur:
                    cur.execute("SET search_path TO logistica, public")
                log.info("Conectado a PostgreSQL %s:%s", self._cfg.pg_host, self._cfg.pg_port)
            except psycopg.OperationalError as exc:
                log.error("PostgreSQL no disponible: %s", exc)
                raise ErrorConexion("PostgreSQL") from exc
        return self._pg

    @contextmanager
    def transaccion(self) -> Iterator:
        """Bloque ACID: si algo falla adentro se hace ROLLBACK de todo; si termina bien, COMMIT.

        Uso:
            with conexiones.transaccion() as cur:
                cur.execute(...)
                cur.execute(...)
        """
        import psycopg

        conexion = self._conexion_pg()
        try:
            with conexion.transaction():
                with conexion.cursor() as cur:
                    yield cur
        except psycopg.OperationalError as exc:
            log.error("Se perdió la conexión con PostgreSQL: %s", exc)
            raise ErrorConexion("PostgreSQL") from exc

    # ------------------------------------------------------------------ MongoDB
    @property
    def mongo(self):
        """Base de datos de MongoDB (objeto pymongo.Database)."""
        if self._mongo_cliente is None:
            from pymongo import MongoClient
            # serverSelectionTimeoutMS: si Mongo no responde en 3 s, error (por defecto espera 30 s)
            self._mongo_cliente = MongoClient(self._cfg.mongo_uri, serverSelectionTimeoutMS=3000,
                                              tz_aware=True)
        return self._mongo_cliente[self._cfg.mongo_db]

    # ------------------------------------------------------------------ Neo4j
    @property
    def neo4j(self):
        """Driver de Neo4j. Se usa con `with conexiones.neo4j.session() as s: ...`."""
        if self._neo4j is None:
            from neo4j import GraphDatabase
            self._neo4j = GraphDatabase.driver(self._cfg.neo4j_uri,
                                               auth=(self._cfg.neo4j_user, self._cfg.neo4j_password),
                                               connection_timeout=5,
                                               # reintentos de una transacción ante fallas transitorias:
                                               # 5 s como máximo (por defecto son 30 s y la consola se "cuelga")
                                               max_transaction_retry_time=5)
        return self._neo4j

    # ------------------------------------------------------------------ Cassandra
    @property
    def cassandra(self):
        """Sesión de Cassandra conectada al keyspace de la aplicación."""
        if self._cassandra_sesion is None:
            from cassandra.cluster import NoHostAvailable
            try:
                self._cassandra_cluster = crear_cluster_cassandra(self._cfg)
                self._cassandra_sesion = self._cassandra_cluster.connect(self._cfg.cassandra_keyspace)
                log.info("Conectado a Cassandra, keyspace %s", self._cfg.cassandra_keyspace)
            except NoHostAvailable as exc:
                self._cassandra_cluster = None
                log.error("Cassandra no disponible: %s", exc)
                raise ErrorConexion("Cassandra") from exc
        return self._cassandra_sesion

    # ------------------------------------------------------------------ Redis
    @property
    def redis(self):
        if self._redis is None:
            import redis
            # decode_responses=True: Redis devuelve str en lugar de bytes
            self._redis = redis.Redis(host=self._cfg.redis_host, port=self._cfg.redis_port,
                                      decode_responses=True, socket_timeout=3, socket_connect_timeout=3)
        return self._redis

    # ------------------------------------------------------------------ Estado
    def verificar(self) -> list[EstadoMotor]:
        """Prueba cada motor con una consulta mínima y mide cuánto tarda."""
        pruebas: list[tuple[str, Callable[[], str]]] = [
            ("PostgreSQL", self._probar_postgres),
            ("MongoDB", self._probar_mongo),
            ("Neo4j", self._probar_neo4j),
            ("Cassandra", self._probar_cassandra),
            ("Redis", self._probar_redis),
        ]
        estados = []
        for motor, prueba in pruebas:
            inicio = time.perf_counter()
            try:
                detalle = prueba()
                estados.append(EstadoMotor(motor, True, detalle, (time.perf_counter() - inicio) * 1000))
            except Exception as exc:  # noqa: BLE001 — se informa cualquier falla de conexión
                log.warning("Verificación de %s falló: %s", motor, exc)
                estados.append(EstadoMotor(motor, False, _resumir_error(exc)))
        return estados

    def _probar_postgres(self) -> str:
        with self.transaccion() as cur:
            cur.execute("SELECT count(*) AS n FROM information_schema.tables WHERE table_schema = 'logistica'")
            n = cur.fetchone()["n"]
        return f"{n} tablas en el esquema logistica"

    def _probar_mongo(self) -> str:
        self.mongo.client.admin.command("ping")
        return f"{len(self.mongo.list_collection_names())} colecciones en {self._cfg.mongo_db}"

    def _probar_neo4j(self) -> str:
        self.neo4j.verify_connectivity()
        with self.neo4j.session() as s:
            n = s.run("SHOW CONSTRAINTS YIELD name RETURN count(*) AS n").single()["n"]
        return f"{n} restricciones de unicidad"

    def _probar_cassandra(self) -> str:
        filas = self.cassandra.execute(
            "SELECT table_name FROM system_schema.tables WHERE keyspace_name = %s",
            (self._cfg.cassandra_keyspace,))
        return f"{len(list(filas))} tablas en {self._cfg.cassandra_keyspace}"

    def _probar_redis(self) -> str:
        self.redis.ping()
        return f"{self.redis.dbsize()} claves"

    # ------------------------------------------------------------------ Cierre
    def cerrar(self) -> None:
        for nombre, cerrar in (
            ("PostgreSQL", lambda: self._pg and self._pg.close()),
            ("MongoDB", lambda: self._mongo_cliente and self._mongo_cliente.close()),
            ("Neo4j", lambda: self._neo4j and self._neo4j.close()),
            ("Cassandra", lambda: self._cassandra_cluster and self._cassandra_cluster.shutdown()),
            ("Redis", lambda: self._redis and self._redis.close()),
        ):
            try:
                cerrar()
            except Exception as exc:  # noqa: BLE001
                log.warning("Error al cerrar %s: %s", nombre, exc)


def crear_cluster_cassandra(cfg: Config, timeout: int = 5):
    """Cluster de Cassandra configurado explícitamente:
    - protocol_version=5: el que habla Cassandra 5.0 (si no, el driver prueba versiones
      más nuevas, falla y baja, y deja advertencias en el log).
    - DCAwareRoundRobinPolicy: reparte las consultas entre los nodos del centro de datos
      local ('datacenter1' es el nombre por defecto de la imagen de Docker).
    """
    from cassandra.cluster import EXEC_PROFILE_DEFAULT, Cluster, ExecutionProfile
    from cassandra.policies import DCAwareRoundRobinPolicy

    # La política va en un "perfil de ejecución" (la forma actual del driver; pasarla
    # directo a Cluster() está obsoleto y deja advertencias)
    perfil = ExecutionProfile(load_balancing_policy=DCAwareRoundRobinPolicy(local_dc=cfg.cassandra_datacenter),
                              request_timeout=10)
    return Cluster([cfg.cassandra_host], port=cfg.cassandra_port, connect_timeout=timeout,
                   protocol_version=5, execution_profiles={EXEC_PROFILE_DEFAULT: perfil})


_SENALES_DE_CAIDA = ("connection refused", "unable to connect", "couldn't connect", "timed out",
                     "serverselectiontimeout", "no host available", "error 111", "error 10061")


def describir_error(exc: Exception) -> str:
    """Traduce un error de driver a un texto corto para el usuario (el detalle va al log)."""
    texto = f"{type(exc).__name__} {exc}".lower()
    if isinstance(exc, ErrorConexion) or any(s in texto for s in _SENALES_DE_CAIDA):
        return "no responde (¿está levantado el contenedor?)"
    if "auth" in texto or "unauthorized" in texto or "password" in texto:
        return "rechazó el usuario o la contraseña (revisá el archivo .env)"
    primera = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
    return primera[:90]


_resumir_error = describir_error
