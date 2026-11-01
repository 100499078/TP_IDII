"""Carga datos de ejemplo para probar y para la defensa.

Uso:  python -m herramientas.datos_demo

Crea, a través de los SERVICIOS (o sea, con todas las validaciones):
  2 clientes, 2 operadores, 1 proveedor, 3 vehículos, 6 contenedores, 3 sensores,
  2 usuarios de ejemplo y 2 envíos (uno con su ruta de 2 tramos y en tránsito).
Si los datos ya existen (se busca el CUIT del primer cliente), no hace nada.
Requiere haber corrido antes  python -m herramientas.inicializar_bases.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from rich.console import Console

from app.config import Config
from app.dominio.modelos import Actor
from app.infra.conexiones import GestorConexiones
from app.logs import configurar_logging
from app.repositorios.postgres.actores import RepositorioActores
from app.repositorios.postgres.catalogos import RepositorioCatalogos
from app.repositorios.postgres.contenedores import RepositorioContenedores
from app.repositorios.postgres.envios import RepositorioEnvios
from app.repositorios.postgres.sesiones import RepositorioHistorialSesiones
from app.repositorios.postgres.usuarios import RepositorioUsuarios
from app.repositorios.redis.sesiones import RepositorioSesionesActivas
from app.servicios.actores import ServicioActores
from app.servicios.autenticacion import ServicioAutenticacion
from app.servicios.catalogos import ServicioCatalogos
from app.servicios.contenedores import ServicioContenedores
from app.servicios.envios import ServicioEnvios
from app.servicios.usuarios import ServicioUsuarios

consola = Console()
CLAVE_DEMO = "Demo12345"


def cargar(cx: GestorConexiones) -> list[str]:
    r_act, r_cat, r_cont = RepositorioActores(cx), RepositorioCatalogos(cx), RepositorioContenedores(cx)
    r_usu = RepositorioUsuarios(cx)
    actores = ServicioActores(r_act)
    if any(a.cuit == "30-71111111-1" for a in actores.listar("CLIENTE")):
        return ["Los datos de ejemplo ya estaban cargados: no se hizo nada."]
    catalogos = ServicioCatalogos(r_cat, r_act)
    contenedores = ServicioContenedores(r_cont)
    envios = ServicioEnvios(RepositorioEnvios(cx), r_act, r_cat, r_cont)
    auth = ServicioAutenticacion(r_usu, RepositorioHistorialSesiones(cx), RepositorioSesionesActivas(cx),
                                 ttl_segundos=1800)
    usuarios = ServicioUsuarios(r_usu, r_act, auth)
    hecho = []

    # Actores
    c1 = actores.crear(Actor(None, "CLIENTE", "Vinos del Sur S.A.", "30-71111111-1", "compras@vinosdelsur.com",
                             "AR", limite_credito=5_000_000, condicion_pago_dias=30))
    c2 = actores.crear(Actor(None, "CLIENTE", "Agro Exportadora Pampa S.A.", "30-71222222-7",
                             "logistica@agropampa.com", "AR", limite_credito=2_000_000, condicion_pago_dias=60))
    o1 = actores.crear(Actor(None, "OPERADOR", "Transportes Andinos S.R.L.", "30-71333333-2",
                             "trafico@tandinos.com", "AR", medios=["TERRESTRE"]))
    o2 = actores.crear(Actor(None, "OPERADOR", "Naviera Atlántica S.A.", "30-71444444-8",
                             "operaciones@navatlantica.com", "UY", medios=["MARITIMO", "TERRESTRE"]))
    actores.crear(Actor(None, "PROVEEDOR", "Sensores IoT Argentina S.A.", "30-71555555-3",
                        "ventas@sensoresiot.com.ar", "AR", rubro="Sensores IoT"))
    hecho.append(f"Clientes {c1}, {c2}; operadores {o1}, {o2}; 1 proveedor")

    # Vehículos
    camion = catalogos.crear_vehiculo("CAMION", "AB123CD", o1)
    buque = catalogos.crear_vehiculo("BUQUE", "IMO9321483", o2)
    catalogos.crear_vehiculo("CAMION", "AE456FG", o2)
    hecho.append("3 vehículos")

    # Contenedores y sensores
    ids = [contenedores.crear_contenedor(cod, tipo, cap) for cod, tipo, cap in [
        ("MSCU1234566", "REEFER", 67.7), ("MSCU2345672", "DRY", 33.2), ("TGHU3456780", "DRY", 67.7),
        ("TRLU4567896", "REEFER", 33.2), ("CMAU5678905", "OPEN_TOP", 32.6), ("MAEU6789017", "TANK", 24.0)]]
    s_temp = contenedores.crear_sensor("TEMPERATURA", "Sensores IoT Argentina", "TX-200", 2, 8)
    s_gps = contenedores.crear_sensor("GPS", "Sensores IoT Argentina", "GPS-10", None, None)
    contenedores.crear_sensor("HUMEDAD", "Sensores IoT Argentina", "HU-5", None, None)
    contenedores.instalar_sensor(s_temp, ids[0])
    contenedores.instalar_sensor(s_gps, ids[0])
    hecho.append(f"6 contenedores ({ids[0]}..{ids[-1]}), 3 sensores (2 instalados en {ids[0]})")

    # Usuarios de ejemplo
    usuarios.crear("Olga", "Operaciones", "operador@logistica.local", CLAVE_DEMO, ["OPERADOR"])
    usuarios.crear("Carlos", "Cliente", "cliente@vinosdelsur.com", CLAVE_DEMO, ["CLIENTE"], id_actor=c1)
    hecho.append(f"Usuarios operador@logistica.local y cliente@vinosdelsur.com (contraseña {CLAVE_DEMO})")

    # Envíos: uno con ruta Rosario → Buenos Aires (camión) → Rotterdam (buque), en tránsito
    ahora = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    e1 = envios.crear_envio(c1, "ARROS", "NLRTM", ahora + timedelta(days=30), 18_000, 60, ids[:2], "ALTA")
    envios.agregar_tramo(e1, "TERRESTRE", "ARBUE", o1, ahora + timedelta(hours=2), ahora + timedelta(hours=10),
                         camion)
    envios.agregar_tramo(e1, "MARITIMO", "NLRTM", o2, ahora + timedelta(days=1), ahora + timedelta(days=25), buque)
    envios.cambiar_estado(e1, "EN_TRANSITO")
    e2 = envios.crear_envio(c2, "ARBUE", "BRSSZ", ahora + timedelta(days=10), 9_500, 40, [ids[2]], "MEDIA")
    hecho.append(f"Envío {e1} (ARROS → ARBUE → NLRTM, EN_TRANSITO) y envío {e2} (CREADO, sin tramos)")
    return hecho


def main() -> int:
    cfg = Config.desde_entorno()
    configurar_logging(cfg.log_carpeta, cfg.log_nivel)
    cx = GestorConexiones(cfg)
    try:
        for linea in cargar(cx):
            consola.print(f"[green]✔[/green] {linea}")
        consola.print("[dim]Los cambios llegan a Neo4j cuando corre el sincronizador "
                      "(la aplicación o  python -m herramientas.sincronizar --una-vez).[/dim]")
        return 0
    finally:
        cx.cerrar()


if __name__ == "__main__":
    sys.exit(main())
