"""Pruebas de configuración y de lectura de scripts."""
import pytest

from app.config import RAIZ_PROYECTO, Config
from app.infra.scripts import dividir_sentencias, leer_sentencias


def test_lee_los_scripts_reales_de_neo4j_y_cassandra():
    cypher = leer_sentencias(RAIZ_PROYECTO / "scripts" / "neo4j" / "01_schema.cypher")
    cql = leer_sentencias(RAIZ_PROYECTO / "scripts" / "cassandra" / "01_schema.cql")
    assert len(cypher) == 14                       # 9 restricciones (fase 4: Incidente) + 5 índices
    assert all(s.startswith("CREATE") for s in cypher)
    assert len(cql) == 7                           # keyspace + USE + 5 tablas
    assert cql[1] == "USE logistica_iot"


def test_dividir_ignora_comentarios():
    texto = "-- comentario\nSELECT 1; -- al final\n\nSELECT 2;"
    assert dividir_sentencias(texto, "--") == ["SELECT 1", "SELECT 2"]


def test_config_por_defecto_coincide_con_docker_compose(monkeypatch, tmp_path):
    for var in ("PG_PORT", "PG_DB", "MONGO_DB", "CASSANDRA_KEYSPACE", "SESION_TTL_SEGUNDOS"):
        monkeypatch.delenv(var, raising=False)
    cfg = Config.desde_entorno(tmp_path / "no_existe.env")
    assert (cfg.pg_port, cfg.pg_db, cfg.mongo_db, cfg.cassandra_keyspace) == (5432, "logistica", "logistica_docs", "logistica_iot")
    assert cfg.sesion_ttl_segundos == 1800


def test_config_rechaza_un_puerto_no_numerico(monkeypatch, tmp_path):
    monkeypatch.setenv("PG_PORT", "abc")
    with pytest.raises(ValueError, match="PG_PORT"):
        Config.desde_entorno(tmp_path / "no_existe.env")
