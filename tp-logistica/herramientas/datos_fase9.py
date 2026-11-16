"""Complemento de los datos de prueba para la fase 9 (lo usa herramientas/datos_prueba.py).

Agrega lo que piden las condiciones de promoción y la carga original no tenía:
  - dos proveedores más, con sus facturas mensuales y los pagos que les hicimos;
  - usuarios con rol SOCIO para un operador logístico y un proveedor, con mensajes;
  - reportes pedidos para clientes (facturables): uno ya cobrado y otros esperando la próxima factura.
Igual que la carga principal, pasa por los SERVICIOS con un reloj simulado. Las facturas de meses
pasados son solo de los proveedores NUEVOS (sus cuentas no existían cuando se cerraron esos meses);
después se cierran esos meses para ellos, así el verificador sigue dando OK.
Si ya se cargó (se busca el CUIT del primer proveedor nuevo), no hace nada.
"""
from __future__ import annotations

import random
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal

from app.dominio.modelos import Actor, Sesion, pesos
from app.excepciones import ErrorAplicacion
from app.infra.conexiones import GestorConexiones
from app.repositorios.mongo.mensajeria import RepositorioConversaciones, RepositorioGrupos, RepositorioMensajes
from app.repositorios.mongo.predicciones import RepositorioPredicciones
from app.repositorios.mongo.reportes import RepositorioReportes
from app.repositorios.mongo.incidentes import RepositorioIncidentes
from app.repositorios.neo4j.grafo import RepositorioGrafo
from app.repositorios.postgres.actores import RepositorioActores
from app.repositorios.postgres.cierres import RepositorioCierres
from app.repositorios.postgres.comercial import RepositorioComercial
from app.repositorios.postgres.reportes import RepositorioReportesSQL
from app.repositorios.postgres.sesiones import RepositorioHistorialSesiones
from app.repositorios.postgres.usuarios import RepositorioUsuarios
from app.repositorios.redis.mensajeria import RepositorioMensajeriaRedis
from app.repositorios.redis.sesiones import RepositorioSesionesActivas
from app.servicios.actores import ServicioActores
from app.servicios.autenticacion import ServicioAutenticacion
from app.servicios.comercial import ServicioComercial
from app.servicios.mensajeria import ServicioMensajeria
from app.servicios.procesos import mes_anterior
from app.servicios.reportes import ServicioReportes
from app.servicios.usuarios import ServicioUsuarios
from herramientas.plan_datos import cuit

CLAVE = "Demo12345"
# clave -> (razón social, 8 dígitos del CUIT o CUIT, email, rubro, importe mensual aproximado)
PROVEEDORES = {
    "combustibles": ("Combustibles del Litoral S.A.", 71800010, "facturacion@combustibleslitoral.com",
                     "Combustible", 900_000),
    "seguros": ("Seguros de Carga S.A.", 71800020, "cobranzas@seguroscarga.com", "Seguros", 450_000),
    "sensores": ("Sensores IoT Argentina S.A.", "30-71555555-3", "ventas@sensoresiot.com.ar", "Sensores IoT",
                 180_000),                                     # el de la demo: solo factura el mes en curso
}
# Usuarios SOCIO: (nombre, apellido, email, CUIT del actor)
SOCIOS = {
    "mariana": ("Mariana", "Costa", "mcosta@navatlantica.com", "30-71444444-8"),        # Naviera Atlántica
    "jorge": ("Jorge", "Ibarra", "jibarra@sensoresiot.com.ar", "30-71555555-3"),       # Sensores IoT
}
CONVERSACIONES = [
    ("mariana", "martin", "Confirmamos el buque de la semana próxima a Rotterdam; necesitamos los contenedores reefer."),
    ("martin", "mariana", "Perfecto, los cargamos el jueves. ¿Mantienen el horario de corte de documentación?"),
    ("mariana", "martin", "Sí, corte el miércoles a las 18. Les mando la factura del mes con el detalle de tramos."),
    ("jorge", "olga", "Les enviamos la factura de los sensores del mes; incluye el recambio de baterías."),
    ("olga", "jorge", "Gracias, la cargamos y la pagamos a 30 días como siempre."),
]
# Reportes para clientes: (clave del cliente en el plan, tipo, días atrás, ¿se factura ya?)
REPORTES = [
    ("frigorifico", "TIEMPOS_POR_CLIENTE", 20, True),
    ("frigorifico", "RIESGO_DEMORA", 3, False),
    ("electro", "ENVIOS_DEMORADOS", 6, False),
    ("biofarma", "DESEMPENO_HISTORICO", 4, False),
]


class RelojSimulado:
    def __init__(self, ahora: datetime):
        self.ahora = ahora

    def __call__(self) -> datetime:
        return self.ahora


def ya_cargado(cx: GestorConexiones) -> bool:
    return any(a.cuit == cuit(PROVEEDORES["combustibles"][1])
               for a in ServicioActores(RepositorioActores(cx)).listar("PROVEEDOR"))


def _sesion(r_usu, email: str, momento: datetime) -> Sesion:
    u, _ = r_usu.buscar_por_email(email)
    return Sesion("carga-fase9", u.id_usuario, u.email, u.nombre_completo, [r.nombre for r in u.roles],
                  u.nivel_permisos, momento, momento, u.id_actor)


def cargar(cx: GestorConexiones, ahora: datetime, meses: int = 6, semilla: int = 2026) -> list[str]:
    if ya_cargado(cx):
        return ["El complemento de la fase 9 ya estaba cargado: no se hizo nada."]
    rng = random.Random(semilla + 9)
    reloj = RelojSimulado(ahora)
    r_act, r_usu = RepositorioActores(cx), RepositorioUsuarios(cx)
    actores = ServicioActores(r_act)
    r_reportes = RepositorioReportes(cx)
    comercial = ServicioComercial(RepositorioComercial(cx), r_act, None, reloj=reloj, repo_reportes=r_reportes)
    auth = ServicioAutenticacion(r_usu, RepositorioHistorialSesiones(cx), RepositorioSesionesActivas(cx), 1800)
    usuarios = ServicioUsuarios(r_usu, r_act, auth)
    hecho = []

    # 1) Proveedores y sus facturas mensuales (con los pagos que les hicimos)
    existentes = {a.cuit: a.id_actor for a in actores.listar()}
    ids = {}
    for clave, (razon, base, email, rubro, _) in PROVEEDORES.items():
        c = cuit(base)
        ids[clave] = existentes.get(c) or actores.crear(Actor(None, "PROVEEDOR", razon, c, email, "AR", rubro=rubro))
    inicio_mes_actual = ahora.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    facturas = pagos = 0
    meses_pasados = []
    for k in range(meses, -1, -1):                        # del mes más viejo al actual
        mes = (inicio_mes_actual - timedelta(days=28 * k)).replace(day=1)
        meses_pasados.append(mes)
    for mes in sorted(set(meses_pasados)):
        for clave, (_, _, _, rubro, monto) in PROVEEDORES.items():
            actual = mes == inicio_mes_actual
            if clave == "sensores" and not actual:
                continue                                  # sus meses pasados ya están cerrados
            emision = datetime.combine(mes.date() + timedelta(days=4), time(11), tzinfo=timezone.utc)
            if emision > ahora:
                continue
            reloj.ahora = emision
            numero = f"{clave[:3].upper()}-{mes:%Y%m}"
            importe = pesos(Decimal(str(round(monto * rng.uniform(0.8, 1.2), -2))))
            id_factura = comercial.registrar_factura_recibida(ids[clave], numero, importe, 30,
                                                              f"{rubro} {mes:%m/%Y}")
            facturas += 1
            pago = emision + timedelta(days=rng.uniform(18, 32))
            if pago <= ahora and not actual:
                reloj.ahora = pago
                comercial.registrar_pago(id_factura, importe, "TRANSFERENCIA")
                pagos += 1
    reloj.ahora = ahora
    comercial.marcar_vencidas()
    cierres = RepositorioCierres(cx)                      # los meses pasados, cerrados también para ellos
    cerradas = 0
    for mes in sorted(set(meses_pasados))[:-1]:
        periodo, desde, hasta = mes_anterior(mes + timedelta(days=32))
        cerradas += cierres.cerrar_periodo(periodo, desde, hasta)
    hecho.append(f"{len(PROVEEDORES)} proveedores con {facturas} facturas y {pagos} pagos ({cerradas} cierres nuevos)")

    # 2) Usuarios SOCIO y mensajes con ellos
    for clave, (nombre, apellido, email, c) in SOCIOS.items():
        if r_usu.buscar_por_email(email) is None:
            usuarios.crear(nombre, apellido, email, CLAVE, ["SOCIO"], existentes.get(c) or ids.get("sensores"))
    mensajeria = ServicioMensajeria(r_usu, RepositorioGrupos(cx), RepositorioConversaciones(cx),
                                    RepositorioMensajes(cx), RepositorioMensajeriaRedis(cx), reloj=reloj)
    emails = {"martin": "trafico@logistica.local", "olga": "operador@logistica.local",
              **{k: v[2] for k, v in SOCIOS.items()}}
    momento = ahora - timedelta(days=9)
    for de, para, texto in CONVERSACIONES:
        momento += timedelta(hours=rng.uniform(2, 30))
        reloj.ahora = momento
        mensajeria.enviar_privado(_sesion(r_usu, emails[de], momento),
                                  _sesion(r_usu, emails[para], momento).id_usuario, texto)
    hecho.append(f"{len(SOCIOS)} usuarios SOCIO (operador y proveedor, contraseña {CLAVE}) y "
                 f"{len(CONVERSACIONES)} mensajes con ellos")

    # 3) Reportes pedidos para clientes (se cobran): uno ya facturado y el resto esperando la próxima factura
    from herramientas.plan_datos import CLIENTES
    por_cuit = {a.cuit: a.id_actor for a in actores.listar("CLIENTE")}
    servicio = ServicioReportes(r_reportes, RepositorioReportesSQL(cx), RepositorioIncidentes(cx),
                                RepositorioGrafo(cx), None, reloj=reloj, repo_predicciones=RepositorioPredicciones(cx))
    admin = r_usu.buscar_por_email("supervision@logistica.local") or r_usu.buscar_por_email("admin@logistica.local")
    a_facturar = set()
    for cliente, tipo, dias, facturar in REPORTES:
        id_cliente = por_cuit[cuit(CLIENTES[cliente][1])]
        reloj.ahora = ahora - timedelta(days=dias, hours=rng.uniform(1, 8))
        id_reporte = servicio.solicitar(admin[0].id_usuario, tipo, id_cliente=id_cliente)
        reloj.ahora += timedelta(minutes=2)
        servicio.procesar_pendientes(id_reporte=id_reporte)
        if facturar:
            a_facturar.add(id_cliente)
    reloj.ahora = ahora
    emitidas = []
    for id_cliente in sorted(a_facturar):
        try:
            f = comercial.facturar_cliente(id_cliente)
            if f:
                emitidas.append(f.numero)
        except ErrorAplicacion as exc:
            hecho.append(f"[yellow]No se pudo facturar al cliente {id_cliente}: {exc}[/yellow]")
    hecho.append(f"{len(REPORTES)} reportes pedidos para clientes; facturados en {', '.join(emitidas) or 'ninguna'}"
                 f", {len(r_reportes.facturables())} esperan la próxima factura")
    return hecho
