"""Inicializa los cinco motores y carga los datos mínimos para entrar a la aplicación.

Uso:  python -m herramientas.inicializar_bases            (crea lo que falte)
      python -m herramientas.inicializar_bases --verificar (solo informa)

Es idempotente: se puede ejecutar todas las veces que haga falta, porque cada
paso comprueba si el objeto ya existe (IF NOT EXISTS, ON CONFLICT, BUSYGROUP).

- PostgreSQL y MongoDB: docker compose ejecuta sus scripts la primera vez que
  crea el volumen (carpeta docker-entrypoint-initdb.d). Si faltan, se avisa.
- Neo4j, Cassandra y Redis: sus imágenes no tienen ese mecanismo, así que los
  scripts se ejecutan desde acá con los drivers de Python.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date

from rich.console import Console
from rich.table import Table

from app.config import RAIZ_PROYECTO, Config
from app.excepciones import ErrorConflicto
from app.infra.conexiones import GestorConexiones, crear_cluster_cassandra, describir_error
from app.infra.scripts import leer_sentencias
from app.logs import configurar_logging
from app.dominio.modelos import Tarifa, Ubicacion
from app.repositorios.postgres.catalogos import RepositorioCatalogos
from app.repositorios.postgres.usuarios import RepositorioUsuarios
from app.servicios import seguridad

SCRIPTS = RAIZ_PROYECTO / "scripts"
consola = Console()
log = logging.getLogger("inicializar")

ROLES = [
    ("ADMINISTRADOR", 5, "Acceso total, incluida la administración de usuarios"),
    ("SUPERVISOR", 4, "Gestión comercial, reportes y procesos periódicos"),
    ("OPERADOR", 3, "Operación diaria: envíos, tramos, contenedores, sensores"),
    ("ANALISTA", 2, "Consultas, reportes y análisis"),
    ("CLIENTE", 1, "Consulta de sus propios envíos y mensajería"),
    ("SOCIO", 1, "Operadores logísticos y proveedores: mensajería, su cuenta corriente y los envíos que operan"),
]
ADMIN_EMAIL = "admin@logistica.local"

# Catálogos mínimos para poder crear envíos (código UN/LOCODE, nombre, tipo, latitud, longitud)
UBICACIONES = [
    ("ARBUE", "Puerto de Buenos Aires", "PUERTO", -34.5833, -58.3667),
    ("AREZE", "Aeropuerto de Ezeiza", "AEROPUERTO", -34.8222, -58.5358),
    ("ARROS", "Centro de distribución Rosario", "CENTRO_DIST", -32.9468, -60.6393),
    ("UYMVD", "Puerto de Montevideo", "PUERTO", -34.9033, -56.2122),
    ("BRSSZ", "Puerto de Santos", "PUERTO", -23.9608, -46.3336),
    ("CLVAP", "Puerto de Valparaíso", "PUERTO", -33.0333, -71.6333),
    ("ESVLC", "Puerto de Valencia", "PUERTO", 39.4436, -0.3167),
    ("NLRTM", "Puerto de Rotterdam", "PUERTO", 51.9500, 4.1333),
    ("USMIA", "Aeropuerto de Miami", "AEROPUERTO", 25.7959, -80.2870),
    ("CNSHA", "Puerto de Shanghái", "PUERTO", 31.2304, 121.4737),
]
# (medio, precio base, precio por kg): costo del tramo = base + kg × precio
TARIFAS = [("TERRESTRE", 15000, 12), ("MARITIMO", 250000, 3.5), ("AEREO", 80000, 45)]


def postgres(cx: GestorConexiones, solo_verificar: bool) -> str:
    with cx.transaccion() as cur:
        cur.execute("SELECT count(*) AS n FROM information_schema.tables WHERE table_schema = 'logistica'")
        tablas = cur.fetchone()["n"]
    if tablas == 0:
        if solo_verificar:
            return "FALTA el esquema logistica"
        with cx.transaccion() as cur:
            cur.execute((SCRIPTS / "postgres" / "01_schema.sql").read_text(encoding="utf-8"))
        tablas = 23
    if solo_verificar:
        return f"{tablas} tablas"
    with cx.transaccion() as cur:             # fase 9: cambios posteriores (idempotente, corre siempre)
        cur.execute((SCRIPTS / "postgres" / "02_fase9.sql").read_text(encoding="utf-8"))
        cur.execute("SELECT count(*) AS n FROM information_schema.tables WHERE table_schema = 'logistica'")
        tablas = cur.fetchone()["n"]
    repo = RepositorioUsuarios(cx)
    for nombre, nivel, descripcion in ROLES:
        repo.asegurar_rol(nombre, nivel, descripcion)
    clave = os.getenv("ADMIN_PASSWORD", "Admin1234")
    try:
        repo.crear("Admin", "Sistema", ADMIN_EMAIL, seguridad.hashear(clave), ["ADMINISTRADOR"])
        admin = f"usuario {ADMIN_EMAIL} creado (contraseña: {clave})"
    except ErrorConflicto:
        admin = f"usuario {ADMIN_EMAIL} ya existía"
    catalogos = RepositorioCatalogos(cx)
    nuevas = sum(catalogos.asegurar_ubicacion(Ubicacion(None, c, n, t, c[:2], la, lo))
                 for c, n, t, la, lo in UBICACIONES)
    for medio, base, kg in TARIFAS:
        catalogos.asegurar_tarifa(Tarifa(None, medio, base, kg, date(2026, 1, 1)))
    return (f"{tablas} tablas, {len(ROLES)} roles, {admin}, "
            f"{len(UBICACIONES)} ubicaciones ({nuevas} nuevas), {len(TARIFAS)} tarifas")


def mongo(cx: GestorConexiones, solo_verificar: bool) -> str:
    colecciones = set(cx.mongo.list_collection_names())
    esperadas = {"eventos_logisticos", "incidentes", "alertas", "grupos_trabajo", "conversaciones",
                 "mensajes", "reportes", "predicciones_riesgo"}
    faltan = esperadas - colecciones
    if faltan:
        return ("FALTAN colecciones: " + ", ".join(sorted(faltan)) +
                ". Ejecutá: docker compose exec mongo mongosh /docker-entrypoint-initdb.d/01_schema.js")
    if not solo_verificar:
        actualizar_validador_reportes(cx.mongo)
    return f"{len(esperadas)} colecciones con validador"


def actualizar_validador_reportes(db) -> None:
    """Fase 9: el validador de `reportes` acepta el tipo RIESGO_DEMORA y los campos de facturación.
    collMod cambia el validador de una colección que ya tiene datos (idempotente)."""
    info = db.command("listCollections", filter={"name": "reportes"})["cursor"]["firstBatch"][0]
    esquema = info["options"]["validator"]["$jsonSchema"]
    propiedades = esquema["properties"]
    tipos = propiedades["tipo"]["enum"]
    if "RIESGO_DEMORA" not in tipos:
        tipos.append("RIESGO_DEMORA")
    propiedades.setdefault("id_cliente", {"bsonType": ["int", "long"]})
    propiedades.setdefault("facturable", {"bsonType": "bool"})
    propiedades.setdefault("id_factura", {"bsonType": ["int", "long"]})
    propiedades.setdefault("numero_factura", {"bsonType": "string"})
    db.command("collMod", "reportes", validator={"$jsonSchema": esquema})


def neo4j(cx: GestorConexiones, solo_verificar: bool) -> str:
    if not solo_verificar:
        with cx.neo4j.session() as s:
            for sentencia in leer_sentencias(SCRIPTS / "neo4j" / "01_schema.cypher"):
                s.run(sentencia).consume()
    with cx.neo4j.session() as s:
        restricciones = s.run("SHOW CONSTRAINTS YIELD name RETURN count(*) AS n").single()["n"]
        # Se excluyen los índices que crean las restricciones y los LOOKUP que Neo4j trae de fábrica
        indices = s.run("SHOW INDEXES YIELD type, owningConstraint "
                        "WHERE owningConstraint IS NULL AND type <> 'LOOKUP' "
                        "RETURN count(*) AS n").single()["n"]
    return f"{restricciones} restricciones de unicidad, {indices} índices"


def cassandra(cfg: Config, solo_verificar: bool) -> str:
    cluster = crear_cluster_cassandra(cfg, timeout=10)
    try:
        sesion = cluster.connect()
        if not solo_verificar:
            for sentencia in leer_sentencias(SCRIPTS / "cassandra" / "01_schema.cql"):
                sesion.execute(sentencia, timeout=60)
        filas = sesion.execute("SELECT table_name FROM system_schema.tables WHERE keyspace_name = %s",
                               (cfg.cassandra_keyspace,))
        return f"{len(list(filas))} tablas en {cfg.cassandra_keyspace}"
    finally:
        cluster.shutdown()


def redis(cx: GestorConexiones, solo_verificar: bool) -> str:
    import redis as libreria_redis
    grupos = [("alertas:stream", "notificadores"), ("sync:stream", "sincronizadores")]
    if not solo_verificar:
        for stream, grupo in grupos:
            try:
                cx.redis.xgroup_create(stream, grupo, id="$", mkstream=True)
            except libreria_redis.ResponseError as exc:
                if "BUSYGROUP" not in str(exc):      # BUSYGROUP = el grupo ya existía
                    raise
    existentes = sum(1 for stream, _ in grupos if cx.redis.exists(stream))
    politica = cx.redis.config_get("maxmemory-policy").get("maxmemory-policy")
    aof = cx.redis.config_get("appendonly").get("appendonly")
    return f"{existentes} streams con grupo de consumidores, AOF={aof}, política={politica}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Inicializa los cinco motores del TP")
    parser.add_argument("--verificar", action="store_true", help="solo informa, no crea nada")
    args = parser.parse_args()

    cfg = Config.desde_entorno()
    configurar_logging(cfg.log_carpeta, cfg.log_nivel)
    cx = GestorConexiones(cfg)
    pasos = [
        ("PostgreSQL", lambda: postgres(cx, args.verificar)),
        ("MongoDB", lambda: mongo(cx, args.verificar)),
        ("Neo4j", lambda: neo4j(cx, args.verificar)),
        ("Cassandra", lambda: cassandra(cfg, args.verificar)),
        ("Redis", lambda: redis(cx, args.verificar)),
    ]
    tabla = Table(title="Verificación de las bases" if args.verificar else "Inicialización de las bases",
                  header_style="bold blue")
    tabla.add_column("Motor")
    tabla.add_column("Resultado")
    fallas = 0
    for motor, paso in pasos:
        with consola.status(f"{motor}..."):
            try:
                resultado = paso()
                ok = not resultado.startswith("FALTA")
            except Exception as exc:  # noqa: BLE001
                log.exception("Falló la inicialización de %s", motor)
                resultado, ok = f"ERROR: {describir_error(exc)}", False
        fallas += 0 if ok else 1
        tabla.add_row(motor, f"[green]{resultado}[/green]" if ok else f"[red]{resultado}[/red]")
    cx.cerrar()
    consola.print(tabla)
    if fallas:
        consola.print("[yellow]Revisá que los contenedores estén 'healthy' con: docker compose ps[/yellow]")
    return 1 if fallas else 0


if __name__ == "__main__":
    sys.exit(main())
