"""Configuración de la aplicación.

Toda la configuración sale de variables de entorno (o del archivo .env).
Así el código no tiene contraseñas ni direcciones escritas a mano, y la
misma aplicación funciona en la PC de cada integrante sin tocar código.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

RAIZ_PROYECTO = Path(__file__).resolve().parent.parent


def _entero(nombre: str, defecto: int) -> int:
    valor = os.getenv(nombre)
    if valor is None or valor.strip() == "":
        return defecto
    try:
        return int(valor)
    except ValueError as exc:
        raise ValueError(f"La variable {nombre} debe ser un número entero (vale '{valor}')") from exc


@dataclass(frozen=True)
class Config:
    # PostgreSQL
    pg_host: str
    pg_port: int
    pg_db: str
    pg_user: str
    pg_password: str
    # MongoDB
    mongo_uri: str
    mongo_db: str
    # Neo4j
    neo4j_uri: str
    neo4j_user: str
    neo4j_password: str
    # Cassandra
    cassandra_host: str
    cassandra_port: int
    cassandra_keyspace: str
    cassandra_datacenter: str
    # Redis
    redis_host: str
    redis_port: int
    # Aplicación
    sesion_ttl_segundos: int
    log_nivel: str
    log_carpeta: Path

    @property
    def pg_dsn(self) -> str:
        return (f"host={self.pg_host} port={self.pg_port} dbname={self.pg_db} "
                f"user={self.pg_user} password={self.pg_password} connect_timeout=5")

    @classmethod
    def desde_entorno(cls, archivo_env: Path | None = None) -> "Config":
        """Lee el .env (si existe) y arma la configuración con valores por defecto
        iguales a los de docker-compose.yml."""
        load_dotenv(archivo_env or RAIZ_PROYECTO / ".env", override=False)
        carpeta_logs = Path(os.getenv("LOG_CARPETA", "logs"))
        if not carpeta_logs.is_absolute():
            carpeta_logs = RAIZ_PROYECTO / carpeta_logs
        return cls(
            pg_host=os.getenv("PG_HOST", "localhost"),
            pg_port=_entero("PG_PORT", 5432),
            pg_db=os.getenv("PG_DB", "logistica"),
            pg_user=os.getenv("PG_USER", "tp"),
            pg_password=os.getenv("PG_PASSWORD", "tp_clave"),
            mongo_uri=os.getenv("MONGO_URI", "mongodb://localhost:27017"),
            mongo_db=os.getenv("MONGO_DB", "logistica_docs"),
            neo4j_uri=os.getenv("NEO4J_URI", "bolt://localhost:7687"),
            neo4j_user=os.getenv("NEO4J_USER", "neo4j"),
            neo4j_password=os.getenv("NEO4J_PASSWORD", "tp_clave_neo4j"),
            cassandra_host=os.getenv("CASSANDRA_HOST", "localhost"),
            cassandra_port=_entero("CASSANDRA_PORT", 9042),
            cassandra_keyspace=os.getenv("CASSANDRA_KEYSPACE", "logistica_iot"),
            cassandra_datacenter=os.getenv("CASSANDRA_DATACENTER", "datacenter1"),
            redis_host=os.getenv("REDIS_HOST", "localhost"),
            redis_port=_entero("REDIS_PORT", 6379),
            sesion_ttl_segundos=_entero("SESION_TTL_SEGUNDOS", 1800),
            log_nivel=os.getenv("LOG_NIVEL", "INFO").upper(),
            log_carpeta=carpeta_logs,
        )
