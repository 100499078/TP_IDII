"""Carga unos meses de historia en los cinco motores, para probar y para la defensa (fase 8).

Uso:  python -m herramientas.datos_prueba              (6 meses, semilla 2026)
      python -m herramientas.datos_prueba --meses 3 --semilla 7

Cómo funciona: una SIMULACIÓN EN EL TIEMPO.
  1. El PLAN (herramientas/plan_datos.py, sin bases) decide qué pasa y cuándo: envíos, tramos,
     demoras, incidentes, facturación, cierres, mensajes. Es una lista de eventos con fecha y hora.
  2. Este módulo recorre esos eventos en orden cronológico. Antes de cada uno adelanta un RELOJ
     SIMULADO hasta esa hora y llama a los SERVICIOS de siempre, con todas sus validaciones.
     Los servicios reciben el reloj por constructor (inyección de dependencias), así que para
     ellos "ahora" es la fecha del evento. Nada se inserta a mano en las tablas.
  3. Lo que pasaría después de hoy no se ejecuta: esos envíos quedan en curso.
  4. Al final se sincroniza (outbox -> Redis -> Neo4j y MongoDB), se calcula el riesgo de los
     envíos en curso y se muestra cuánto quedó en cada motor.

Se SUMA a los datos de la demo (si faltan, los carga primero). Si ya se cargó, no hace nada.
Necesita los cinco motores levantados (docker compose up -d) y haber corrido inicializar_bases.
"""
from __future__ import annotations

import argparse
import heapq
import random
import sys
import time
from collections import Counter
from datetime import datetime, time as hora, timedelta, timezone

from rich.console import Console

from app.__main__ import construir_sincronizacion
from app.config import Config
from app.dominio.modelos import MEDIOS_PAGO, Actor, Sesion, pesos
from app.excepciones import ErrorAplicacion, ErrorConexion
from app.infra.conexiones import GestorConexiones
from app.logs import configurar_logging
from app.repositorios.cassandra.telemetria import RepositorioTelemetria
from app.repositorios.mongo.alertas import RepositorioAlertas
from app.repositorios.mongo.eventos import RepositorioEventos
from app.repositorios.mongo.incidentes import RepositorioIncidentes
from app.repositorios.mongo.mensajeria import RepositorioConversaciones, RepositorioGrupos, RepositorioMensajes
from app.repositorios.mongo.predicciones import RepositorioPredicciones
from app.repositorios.neo4j.grafo import RepositorioGrafo
from app.repositorios.postgres.actores import RepositorioActores
from app.repositorios.postgres.catalogos import RepositorioCatalogos
from app.repositorios.postgres.cierres import RepositorioCierres
from app.repositorios.postgres.comercial import RepositorioComercial
from app.repositorios.postgres.contenedores import RepositorioContenedores
from app.repositorios.postgres.envios import RepositorioEnvios
from app.repositorios.postgres.reportes import RepositorioReportesSQL
from app.repositorios.postgres.sesiones import RepositorioHistorialSesiones
from app.repositorios.postgres.usuarios import RepositorioUsuarios
from app.repositorios.redis.cache import RepositorioCache
from app.repositorios.redis.mensajeria import RepositorioMensajeriaRedis
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
from app.servicios.riesgo import ServicioRiesgo
from app.servicios.simulacion import SimuladorSensores
from app.servicios.telemetria import ServicioTelemetria
from app.servicios.trazabilidad import ServicioTrazabilidad
from app.servicios.usuarios import ServicioUsuarios
from herramientas import datos_demo
from herramientas.plan_datos import (CLIENTES, OPERADORES, UBICACIONES_EXTRA, USUARIOS, USUARIOS_DEMO, Plan,
                                     armar_plan, cuit)

consola = Console()
CLAVE = datos_demo.CLAVE_DEMO
PRIMER_CLIENTE_NUEVO = "frigorifico"         # si su CUIT ya existe, la carga ya se hizo

DESCRIPCION_INCIDENTE = {
    "TEMPERATURA_FUERA_DE_RANGO": "La temperatura del contenedor salió del rango durante el viaje",
    "DANIO_CARGA": "Se detectaron bultos dañados en la inspección",
    "DEMORA_ADUANA": "La carga quedó retenida en aduana para inspección",
    "ROBO": "Faltante de mercadería informado por el transportista",
    "CLIMA": "Mal tiempo: la salida o el tránsito se demoran",
    "FALLA_VEHICULO": "Desperfecto mecánico del vehículo",
    "DOCUMENTACION": "Falta o tiene errores la documentación de la carga",
    "OTRO": "Novedad informada por el operador",
}


class RelojSimulado:
    """Un reloj que se mueve a mano. Los servicios lo llaman como a cualquier reloj: reloj()."""

    def __init__(self, inicio: datetime):
        self.ahora = inicio

    def __call__(self) -> datetime:
        return self.ahora


class MensajeriaRedisSinLimite(RepositorioMensajeriaRedis):
    """El límite de 20 mensajes por minuto es de TIEMPO REAL (una clave de Redis con TTL de 60 s),
    y la carga simula semanas en segundos. Para la carga se inyecta esta versión, igual a la real
    pero sin el límite: otra ventaja de recibir los repositorios por constructor."""

    def permitir_envio(self, id_usuario: int) -> bool:
        return True


class Cargador:
    def __init__(self, cx: GestorConexiones, plan: Plan, semilla: int):
        self.cx, self.plan = cx, plan
        self.reloj = RelojSimulado(plan.inicio)
        self.rng = random.Random(semilla + 1)
        r_act, r_cat, r_cont = RepositorioActores(cx), RepositorioCatalogos(cx), RepositorioContenedores(cx)
        r_env, r_usu = RepositorioEnvios(cx), RepositorioUsuarios(cx)
        self.r_cat, self.r_usu = r_cat, r_usu
        self.actores = ServicioActores(r_act)
        self.catalogos = ServicioCatalogos(r_cat, r_act)
        self.contenedores = ServicioContenedores(r_cont)
        # Todos los servicios que registran fechas reciben el reloj simulado
        self.envios = ServicioEnvios(r_env, r_act, r_cat, r_cont, reloj=self.reloj)
        self.telemetria = ServicioTelemetria(r_cont, RepositorioTelemetria(cx), RepositorioTelemetriaRedis(cx),
                                             RepositorioAlertas(cx), reloj=self.reloj)
        self.simulador = SimuladorSensores(self.telemetria, r_cont, r_env, r_cat, prob_anomalia=0.01,
                                           semilla=semilla, reloj=self.reloj)
        self.trazabilidad = ServicioTrazabilidad(r_env, r_cat, RepositorioEventos(cx), RepositorioIncidentes(cx),
                                                 RepositorioCache(cx), reloj=self.reloj)
        self.comercial = ServicioComercial(RepositorioComercial(cx), r_act, None, reloj=self.reloj)
        self.cierres = RepositorioCierres(cx)
        self.mensajeria = ServicioMensajeria(r_usu, RepositorioGrupos(cx), RepositorioConversaciones(cx),
                                             RepositorioMensajes(cx), MensajeriaRedisSinLimite(cx), reloj=self.reloj)
        auth = ServicioAutenticacion(r_usu, RepositorioHistorialSesiones(cx), RepositorioSesionesActivas(cx), 1800)
        self.usuarios = ServicioUsuarios(r_usu, r_act, auth)
        # Lo que se va creando: clave del plan -> ID en la base
        self.id_cliente: dict[str, int] = {}
        self.cliente_de: dict[int, str] = {}
        self.id_operador: dict[str, int] = {}
        self.id_vehiculo: dict[str, int] = {}
        self.id_contenedor: dict[int, int] = {}
        self.sesion: dict[str, Sesion] = {}
        self.id_envio: dict[int, int] = {}
        self.id_tramo: dict[tuple[int, int], int] = {}
        self.id_incidente: dict[tuple[int, int], str] = {}
        self.conversacion_grupo: str | None = None
        self.cuenta: Counter = Counter()
        self.errores: list[str] = []
        self._cola: list = []
        self._seq = 0

    # ================================================================== 1. catálogos y actores
    def preparar(self) -> list[str]:
        hecho = []
        nuevas = 0
        for codigo, nombre, tipo, lat, lon in UBICACIONES_EXTRA:
            if self.r_cat.ubicacion(codigo) is None:
                self.catalogos.crear_ubicacion(codigo, nombre, tipo, lat, lon)
                nuevas += 1
        hecho.append(f"{nuevas} ubicaciones nuevas")

        existentes = {a.cuit: a.id_actor for a in self.actores.listar()}
        for clave, (razon, cuit_base, email, pais, medios, vehiculos) in OPERADORES.items():
            c = cuit(cuit_base)
            self.id_operador[clave] = existentes.get(c) or self.actores.crear(
                Actor(None, "OPERADOR", razon, c, email, pais, medios=medios))
            propios = {v.identificacion: v.id_vehiculo for v in self.catalogos.listar_vehiculos(self.id_operador[clave])}
            for tipo, ident in vehiculos:
                self.id_vehiculo[ident] = propios.get(ident) or self.catalogos.crear_vehiculo(
                    tipo, ident, self.id_operador[clave])
        for clave, datos in CLIENTES.items():
            razon, cuit_base, email, pais, limite, dias = datos[:6]
            c = cuit(cuit_base)
            self.id_cliente[clave] = existentes.get(c) or self.actores.crear(
                Actor(None, "CLIENTE", razon, c, email, pais, limite_credito=limite, condicion_pago_dias=dias))
            self.cliente_de[self.id_cliente[clave]] = clave
        hecho.append(f"{len(self.id_operador)} operadores con {len(self.id_vehiculo)} vehículos, "
                     f"{len(self.id_cliente)} clientes")

        sensores = 0
        existentes = {c.codigo_internacional: c.id_contenedor for c in self.contenedores.listar_contenedores()}
        for i, k in enumerate(self.plan.contenedores):
            if k.codigo in existentes:                    # (por si una carga anterior se cortó acá)
                self.id_contenedor[i] = existentes[k.codigo]
                continue
            self.id_contenedor[i] = self.contenedores.crear_contenedor(k.codigo, k.tipo, k.capacidad)
            for tipo in k.sensores:
                minimo, maximo = k.rango if tipo == "TEMPERATURA" else (None, None)
                modelo = "TX-200" if tipo == "TEMPERATURA" else "GPS-10"
                s = self.contenedores.crear_sensor(tipo, "Sensores IoT Argentina", modelo, minimo, maximo)
                self.contenedores.instalar_sensor(s, self.id_contenedor[i], self.plan.inicio.date())
                sensores += 1
        hecho.append(f"{len(self.id_contenedor)} contenedores con {sensores} sensores")

        for clave, (nombre, apellido, email, rol, cliente) in USUARIOS.items():
            if self.r_usu.buscar_por_email(email) is None:
                self.usuarios.crear(nombre, apellido, email, CLAVE, [rol], self.id_cliente.get(cliente))
        for clave, email in {**USUARIOS_DEMO, **{k: v[2] for k, v in USUARIOS.items()}}.items():
            u, _ = self.r_usu.buscar_por_email(email)
            self.sesion[clave] = Sesion("carga-datos-prueba", u.id_usuario, u.email, u.nombre_completo,
                                        [r.nombre for r in u.roles], u.nivel_permisos, self.plan.inicio,
                                        self.plan.inicio, u.id_actor)
        hecho.append(f"{len(USUARIOS)} usuarios nuevos (contraseña {CLAVE})")
        return hecho

    # ================================================================== 2. la simulación
    def programar(self, momento: datetime, tipo: str, datos: dict, prioridad: int = 5) -> None:
        """Agrega un evento a la cola (solo si no es futuro). Lo usan los pagos, que se deciden al facturar."""
        if momento <= self.plan.ahora:
            self._seq += 1
            heapq.heappush(self._cola, (momento, prioridad, 1_000_000 + self._seq, tipo, datos))

    def ejecutar(self) -> None:
        for i, e in enumerate(self.plan.eventos_hasta(self.plan.ahora)):        # el plan ya viene ordenado
            heapq.heappush(self._cola, (e.momento, 0, i, e.tipo, e.datos))
        total, hechos, aviso = len(self._cola), 0, time.monotonic()
        while self._cola:
            momento, _, _, tipo, datos = heapq.heappop(self._cola)
            self.reloj.ahora = momento                                   # <- el "viaje en el tiempo"
            try:
                getattr(self, f"_{tipo.lower()}")(datos)
                self.cuenta[tipo] += 1
            except ErrorAplicacion as exc:
                self.errores.append(f"{momento:%Y-%m-%d %H:%M} {tipo} {datos.get('envio', '')}: {exc}")
            hechos += 1
            if time.monotonic() - aviso > 5:
                consola.print(f"  [dim]{momento:%Y-%m-%d} · {hechos} de {total}+ eventos · "
                              f"{self.cuenta['lecturas']} lecturas[/dim]")
                aviso = time.monotonic()

    # ------------------------------------------------------------------ envíos
    def _crear(self, d: dict) -> None:
        e = self.plan.envios[d["envio"] - 1]
        id_envio = self.envios.crear_envio(self.id_cliente[e.cliente], e.origen, e.destino, e.entrega_estimada,
                                           e.peso_kg, e.volumen_m3, [self.id_contenedor[i] for i in e.contenedores],
                                           e.prioridad)
        self.id_envio[e.n] = id_envio
        for i, t in enumerate(e.tramos):
            self.id_tramo[(e.n, i)] = self.envios.agregar_tramo(
                id_envio, t.medio, t.destino, self.id_operador[t.operador], t.salida, t.llegada_estimada,
                self.id_vehiculo[t.vehiculo])

    def _cancelar(self, d: dict) -> None:
        self.envios.cambiar_estado(self.id_envio[d["envio"]], "CANCELADO")

    def _estado(self, d: dict) -> None:
        self.envios.cambiar_estado(self.id_envio[d["envio"]], d["estado"])

    def _llegada(self, d: dict) -> None:
        t = self.plan.envios[d["envio"] - 1].tramos[d["tramo"]]
        self.envios.registrar_llegada(self.id_tramo[(d["envio"], d["tramo"])], t.llegada_real)

    def _lecturas(self, d: dict) -> None:
        e = self.plan.envios[d["envio"] - 1]
        r = self.simulador.generar(d["momentos"], contenedores={self.id_contenedor[i] for i in e.contenedores})
        self.cuenta["lecturas"] += r.lecturas
        self.cuenta["alertas"] += len(r.alertas)
        if r.errores:
            self.errores.append(f"{d['momentos'][-1]:%Y-%m-%d} {r.errores} lecturas rechazadas (envío {e.n})")

    # ------------------------------------------------------------------ incidentes
    def _incidente(self, d: dict) -> None:
        sesion = self.sesion[self.rng.choice(["olga", "martin"])]
        self.id_incidente[(d["envio"], d["incidente"])] = self.trazabilidad.registrar_incidente(
            self.id_envio[d["envio"]], d["tipo"], d["severidad"], DESCRIPCION_INCIDENTE[d["tipo"]], sesion,
            self.id_tramo[(d["envio"], d["tramo"])])

    def _incidente_estado(self, d: dict) -> None:
        self.trazabilidad.cambiar_estado_incidente(self.id_incidente[(d["envio"], d["incidente"])], d["estado"],
                                                   self.sesion["martin"])

    # ------------------------------------------------------------------ comercial
    def _medio(self) -> str:
        return self.rng.choices(MEDIOS_PAGO, weights=[70, 10, 15, 5])[0]

    def _cuando(self, fecha_emision, dias: float) -> datetime:
        base = datetime.combine(fecha_emision, hora(10), tzinfo=timezone.utc)
        return (base + timedelta(days=dias, hours=self.rng.uniform(0, 7))).replace(second=0, microsecond=0)

    def _facturar(self, d: dict) -> None:
        """Facturación del mes; cómo y cuándo paga cada factura depende del cliente."""
        for f in self.comercial.facturar_todos():
            self.cuenta["facturas"] += 1
            clave = self.cliente_de.get(f.id_actor)
            comportamiento = CLIENTES[clave][9] if clave else "NORMAL"
            dias = CLIENTES[clave][5] if clave else 30
            total, azar = f.importe_total, self.rng.random()
            if comportamiento == "PUNTUAL" or (comportamiento == "NORMAL" and azar < 0.7):
                plazo = self.rng.uniform(3, dias - 2) if comportamiento == "PUNTUAL" else self.rng.uniform(10, dias + 8)
                self.programar(self._cuando(f.fecha_emision, plazo), "PAGO", {"factura": f.id_factura, "monto": total})
            elif comportamiento == "NORMAL" or azar < 0.6:              # paga una parte y el resto, tarde
                parte = pesos(total * pesos(self.rng.choice(["0.3", "0.4", "0.5"])))
                self.programar(self._cuando(f.fecha_emision, self.rng.uniform(10, 20)), "PAGO",
                               {"factura": f.id_factura, "monto": parte})
                self.programar(self._cuando(f.fecha_emision, dias + self.rng.uniform(5, 30)), "PAGO",
                               {"factura": f.id_factura, "monto": total - parte})
            # (el resto de los morosos no paga: la factura queda VENCIDA)

    def _pago(self, d: dict) -> None:
        self.comercial.registrar_pago(d["factura"], d["monto"], self._medio())
        self.cuenta["pagos"] += 1

    def _vencidas(self, d: dict) -> None:
        self.cuenta["facturas vencidas"] += self.comercial.marcar_vencidas()

    def _cierre(self, d: dict) -> None:
        self.cuenta["cuentas cerradas"] += self.cierres.cerrar_periodo(d["periodo"], d["desde"], d["hasta"])

    def _recibidas(self, d: dict) -> None:
        """Cada operador nos factura el 70 % de lo que cobramos por sus tramos del mes anterior."""
        with self.cx.transaccion() as cur:
            cur.execute("""SELECT id_operador, sum(costo) AS total FROM tramo
                            WHERE fecha_salida >= %s AND fecha_salida < %s GROUP BY id_operador""",
                        (d["desde"], d["hasta"]))
            totales = {f["id_operador"]: f["total"] for f in cur.fetchall()}
        for clave, id_op in self.id_operador.items():
            if not totales.get(id_op):
                continue
            importe = pesos(totales[id_op] * pesos("0.7"))
            numero = f"{clave[:3].upper()}-{d['periodo'].replace('-', '')}"
            id_factura = self.comercial.registrar_factura_recibida(id_op, numero, importe, 30,
                                                                   f"Servicios de transporte {d['periodo']}")
            self.cuenta["facturas recibidas"] += 1
            hoy = self.reloj.ahora.date()
            self.programar(self._cuando(hoy, self.rng.uniform(20, 30)), "PAGO", {"factura": id_factura, "monto": importe})

    # ------------------------------------------------------------------ mensajería
    def _grupo(self, d: dict) -> None:
        paula = self.sesion["paula"]
        id_grupo = self.mensajeria.crear_grupo(paula, "Coordinación de exportaciones",
                                               "Novedades de envíos, aduanas y facturación",
                                               [self.sesion[k].id_usuario for k in ("olga", "martin", "diego")])
        self.conversacion_grupo = next(f["conversacion"].id_conversacion for f in self.mensajeria.bandeja(paula)
                                       if f["conversacion"].id_grupo == id_grupo)

    def _mensaje_grupo(self, d: dict) -> None:
        self.mensajeria.enviar(self.conversacion_grupo, d["texto"], self.sesion[d["autor"]])
        self.cuenta["mensajes"] += 1

    def _mensaje_privado(self, d: dict) -> None:
        texto = d["texto"].format(id=self.id_envio[d["envio"]])
        self.mensajeria.enviar_privado(self.sesion[d["de"]], self.sesion[d["para"]].id_usuario, texto)
        self.cuenta["mensajes"] += 1

    # ================================================================== 3. al final
    def terminar(self) -> list[str]:
        hecho = []
        # Las alertas viejas ya no son "nuevas": se dan por vistas en el stream y por resueltas en MongoDB
        while self.telemetria.alertas_nuevas("carga-datos-prueba"):
            pass
        limite = self.plan.ahora - timedelta(days=2)
        viejas = [a for a in self.telemetria.alertas() if a.fecha < limite]
        for a in viejas:
            self.telemetria.cambiar_estado_alerta(a.id_alerta, "RESUELTA")
        hecho.append(f"{len(viejas)} alertas de más de 2 días marcadas RESUELTA")

        try:
            sync, publicados, aplicados = construir_sincronizacion(self.cx), 0, 0
            for _ in range(1000):
                p, a = sync.ciclo()
                publicados, aplicados = publicados + p, aplicados + a
                if p == 0 and a == 0:
                    break
            hecho.append(f"Sincronización: {publicados} eventos publicados en Redis, {aplicados} aplicados "
                         "en Neo4j y MongoDB")
        except ErrorConexion as exc:
            hecho.append(f"[yellow]Sincronización pendiente ({exc}): la hace el sincronizador después[/yellow]")
        try:
            riesgo = ServicioRiesgo(RepositorioReportesSQL(self.cx), RepositorioGrafo(self.cx),
                                    RepositorioIncidentes(self.cx), RepositorioPredicciones(self.cx))
            hecho.append(f"Riesgo: {riesgo.calcular_todos()}")
        except ErrorConexion as exc:
            hecho.append(f"[yellow]Riesgo no calculado ({exc})[/yellow]")
        return hecho


def ya_cargado(cx: GestorConexiones) -> bool:
    return any(a.cuit == cuit(CLIENTES[PRIMER_CLIENTE_NUEVO][1])
               for a in ServicioActores(RepositorioActores(cx)).listar("CLIENTE"))


def complemento_fase9(cx: GestorConexiones, args) -> None:
    """Fase 9: proveedores con facturas, usuarios SOCIO y reportes facturables (herramientas/datos_fase9.py)."""
    from herramientas import datos_fase9
    for linea in datos_fase9.cargar(cx, datetime.now(timezone.utc), args.meses, args.semilla):
        consola.print(f"[green]✔[/green] Fase 9: {linea}")
    try:
        sync = construir_sincronizacion(cx)
        for _ in range(100):
            if sync.ciclo() == (0, 0):
                break
    except ErrorConexion as exc:
        consola.print(f"[yellow]Sincronización pendiente ({exc})[/yellow]")


def main() -> int:
    parser = argparse.ArgumentParser(description="Carga meses de historia de prueba en los cinco motores")
    parser.add_argument("--meses", type=int, default=6, choices=range(1, 13), metavar="1-12")
    parser.add_argument("--semilla", type=int, default=2026)
    args = parser.parse_args()

    cfg = Config.desde_entorno()
    configurar_logging(cfg.log_carpeta, cfg.log_nivel)
    cx = GestorConexiones(cfg)
    try:
        caidos = [m.motor for m in ServicioEstado(cx).verificar() if not m.ok]
        if caidos:
            consola.print(f"[red]No responden: {', '.join(caidos)}. Levantá los motores (docker compose up -d).[/red]")
            return 1
        for linea in datos_demo.cargar(cx):                     # la demo primero (no hace nada si ya está)
            consola.print(f"[dim]Demo: {linea}[/dim]")
        if ya_cargado(cx):
            consola.print("Los datos de prueba ya estaban cargados.")
            complemento_fase9(cx, args)                  # fase 9: se agrega aunque la carga principal ya esté
            return 0
        inicio = time.monotonic()
        plan = armar_plan(datetime.now(timezone.utc), args.meses, args.semilla)
        consola.print(f"Plan: {len(plan.envios)} envíos entre {plan.inicio:%d/%m/%Y} y hoy, "
                      f"{len(plan.eventos_hasta(plan.ahora))} eventos, lecturas cada {plan.paso_lecturas_h} h")
        cargador = Cargador(cx, plan, args.semilla)
        for linea in cargador.preparar():
            consola.print(f"[green]✔[/green] {linea}")
        consola.print("Simulando la historia (puede tardar un minuto)...")
        cargador.ejecutar()
        c = cargador.cuenta
        consola.print(f"[green]✔[/green] {c['CREAR']} envíos, {c['LLEGADA']} llegadas de tramos, "
                      f"{c['ESTADO'] + c['CANCELAR']} cambios de estado, {c['INCIDENTE']} incidentes, "
                      f"{c['lecturas']} lecturas IoT ({c['alertas']} alertas)")
        consola.print(f"[green]✔[/green] {c['facturas']} facturas emitidas, {c['facturas recibidas']} recibidas, "
                      f"{c['pagos']} pagos, {c['facturas vencidas']} vencidas, {c['cuentas cerradas']} cierres de "
                      f"cuenta, {c['mensajes']} mensajes")
        for linea in cargador.terminar():
            consola.print(f"[green]✔[/green] {linea}")
        complemento_fase9(cx, args)
        if cargador.errores:
            consola.print(f"[yellow]{len(cargador.errores)} eventos rechazados por las validaciones:[/yellow]")
            for error in cargador.errores[:10]:
                consola.print(f"  [yellow]{error}[/yellow]")
        consola.print(f"Listo en {time.monotonic() - inicio:.0f} s. Controlá todo con: python -m herramientas.verificar")
        return 0
    finally:
        cx.cerrar()


if __name__ == "__main__":
    sys.exit(main())
