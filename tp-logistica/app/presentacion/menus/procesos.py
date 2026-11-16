"""Menú de reportes, predicción de riesgo y procesos periódicos (fase 7)."""
from __future__ import annotations

from rich.markup import escape

from app.dominio.modelos import PRECIO_REPORTE, Sesion
from app.excepciones import ErrorPermiso
from app.presentacion import componentes as ui
from app.presentacion.menus.base import MenuBase
from app.servicios.reportes import SOLO_INTERNOS, TIPOS_REPORTE

COLOR_NIVEL = {"BAJO": "green", "MEDIO": "yellow", "ALTO": "red", "CRITICO": "bold red"}


def _celda(valor) -> str:
    if isinstance(valor, float):
        return f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    if isinstance(valor, list):
        return ", ".join(map(str, valor))
    if hasattr(valor, "strftime"):
        return ui.fecha(valor)
    return escape(str(valor)) if valor is not None else "—"


class MenuProcesos(MenuBase):
    TITULO = "Reportes, riesgo y procesos"
    OPCIONES = [
        # Fase 9: los clientes también piden reportes (de sus datos, y se les cobran): nivel 1
        ("1", "Pedir un reporte", "pedir", 1),
        ("2", "Mis reportes", "mis_reportes", 1),
        ("3", "Ver un reporte", "ver", 1),
        ("4", "Exportar un reporte a CSV o JSON", "exportar", 1),
        ("5", "Envíos con más riesgo de demora", "riesgo", 2),
        ("6", "Historia de riesgo de un envío", "historia_riesgo", 2),
        ("7", "Estado de los procesos periódicos", "estado", 4),
        ("8", "Correr un proceso ahora", "correr", 4),
        ("9", "Cierres mensuales de una cuenta", "cierres", 4),
    ]

    def __init__(self, ejecutar, sesion_actual, fase7):
        super().__init__(ejecutar, sesion_actual)
        self._f = fase7

    # ------------------------------------------------------------------ reportes
    def pedir(self, sesion: Sesion) -> None:
        if "SOCIO" in sesion.roles:
            raise ErrorPermiso("Los reportes son para clientes y personal de la empresa")
        es_cliente = "CLIENTE" in sesion.roles
        claves = [k for k in TIPOS_REPORTE if not (es_cliente and k in SOLO_INTERNOS)]
        ui.tabla(["#", "Reporte"], [(n, TIPOS_REPORTE[k]) for n, k in enumerate(claves, 1)], "Reportes disponibles")
        tipo = claves[ui.pedir_entero("Número de reporte", minimo=1, maximo=len(claves)) - 1]
        desde = hasta = None
        if ui.confirmar("¿Filtrar por fechas?", defecto=False):
            desde = ui.pedir_fecha_hora("Desde").date()
            hasta = ui.pedir_fecha_hora("Hasta").date()
        # Fase 9: reporte de un cliente = solo sus datos y se le cobra en su próxima factura
        if es_cliente:
            id_cliente = sesion.id_actor
            ui.info(f"El reporte tiene un costo de {ui.dinero(PRECIO_REPORTE)} + IVA, que se suma a tu próxima factura.")
        else:
            id_cliente = ui.pedir_entero("¿Es para un cliente? ID del cliente (Enter = uso interno, no se cobra)",
                                         minimo=1, obligatorio=False)
        id_reporte = self._f.reportes.solicitar(sesion.id_usuario, tipo, desde, hasta, id_cliente=id_cliente)
        ui.ok(f"Reporte pedido (ID {id_reporte[-8:]}): quedó en la cola de MongoDB como SOLICITADO")
        if ui.confirmar("¿Generarlo ahora, sin esperar al planificador?", defecto=True):
            ui.info(self._f.reportes.procesar_pendientes(id_reporte=id_reporte))
            self._mostrar(self._f.reportes.obtener(id_reporte, sesion.id_usuario))

    def mis_reportes(self, sesion: Sesion) -> list[dict]:
        lista = self._f.reportes.mis_reportes(sesion.id_usuario)
        ui.tabla(["#", "ID", "Reporte", "Pedido (UTC)", "Estado", "Generado (UTC)"],
                 [(n, r["id_reporte"][-8:], TIPOS_REPORTE.get(r["tipo"], r["tipo"]), ui.fecha(r["fecha_solicitud"]),
                   r["estado"], ui.fecha(r.get("fecha_generacion"))) for n, r in enumerate(lista, 1)],
                 "Mis reportes (MongoDB, sin el resultado)")
        return lista

    def _elegir(self, sesion: Sesion) -> dict | None:
        lista = self.mis_reportes(sesion)
        if not lista:
            return None
        elegido = lista[ui.pedir_entero("Número de reporte", minimo=1, maximo=len(lista)) - 1]
        return self._f.reportes.obtener(elegido["id_reporte"], sesion.id_usuario)

    def _mostrar(self, r: dict) -> None:
        if r["estado"] != "GENERADO":
            ui.error(f"El reporte está {r['estado']}" + (f": {r.get('error')}" if r.get("error") else ""))
            return
        filas = r.get("resultado", [])
        columnas = list(dict.fromkeys(k for f in filas for k in f))
        ui.tabla(columnas, [[_celda(f.get(c)) for c in columnas] for f in filas],
                 f"{TIPOS_REPORTE.get(r['tipo'], r['tipo'])} · {len(filas)} filas")

    def ver(self, sesion: Sesion) -> None:
        r = self._elegir(sesion)
        if r:
            self._mostrar(r)

    def exportar(self, sesion: Sesion) -> None:
        r = self._elegir(sesion)
        if r:
            formato = ui.pedir_opcion("Formato", ["CSV", "JSON"], defecto="CSV")
            ui.ok(f"Guardado en {self._f.reportes.exportar(r, formato)}")

    # ------------------------------------------------------------------ riesgo
    def riesgo(self, sesion: Sesion) -> None:
        filas = self._f.riesgo.actuales()
        ui.tabla(["Envío", "Nivel", "Prob. demora", "Operativo", "Climático", "Calculado (UTC)", "Por qué"],
                 [(p["id_envio"], f"[{COLOR_NIVEL[p['nivel']]}]{p['nivel']}[/]", f"{p['probabilidad_demora']:.0%}",
                   f"{p['riesgo_operativo']:.2f}", f"{p['riesgo_climatico']:.2f}", ui.fecha(p["fecha_calculo"]),
                   self._motivo(p["variables"])) for p in filas],
                 "Última predicción de cada envío en curso (MongoDB, agregación $group + $first)")
        if not filas:
            ui.info("Todavía no hay predicciones: corré el proceso 'riesgo' (opción 8) o el planificador.")

    @staticmethod
    def _motivo(v: dict) -> str:
        motivos = []
        if v.get("ya_atrasado"):
            motivos.append("ya pasó la fecha comprometida")
        if v.get("plan_llega_tarde"):
            motivos.append("lo planificado llega tarde")
        if v.get("incidentes_abiertos"):
            motivos.append(f"{v['incidentes_abiertos']} incidentes abiertos")
        if v.get("tasa_incidentes_ruta"):
            motivos.append(f"ruta con {v['tasa_incidentes_ruta']:.0%} de incidentes")
        if v.get("sin_datos_de_rutas"):
            motivos.append("sin datos del grafo")
        return "; ".join(motivos) or "sin señales"

    def historia_riesgo(self, sesion: Sesion) -> None:
        filas = self._f.riesgo.historia(ui.pedir_entero("ID del envío", minimo=1))
        ui.tabla(["Calculado (UTC)", "Nivel", "Prob. demora"],
                 [(ui.fecha(p["fecha_calculo"]), p["nivel"], f"{p['probabilidad_demora']:.0%}") for p in filas],
                 "Cómo cambió el riesgo (una predicción por corrida)")

    # ------------------------------------------------------------------ procesos
    def estado(self, sesion: Sesion) -> None:
        filas = []
        for f in self._f.procesos.estado():
            p, u = f["proceso"], f["ultimo"]
            filas.append((p.nombre, p.descripcion, _intervalo(p.cada),
                          ui.fecha(u["inicio"]) if u else "nunca",
                          ("[green]OK[/green] " if u["ok"] else "[red]ERROR[/red] ") + escape(u["resultado"]) if u else "—",
                          ui.fecha(f["proxima"])))
        ui.tabla(["Proceso", "Qué hace", "Cada", "Última vez (UTC)", "Resultado", "Próxima (UTC)"], filas,
                 "Procesos periódicos (última ejecución guardada en Redis)")

    def correr(self, sesion: Sesion) -> None:
        nombres = list(self._f.procesos.procesos)
        nombre = ui.pedir_opcion("Proceso", nombres)
        with ui.consola.status(f"Ejecutando {nombre}..."):
            r = self._f.procesos.ejecutar(nombre)
        (ui.ok if r.ok else ui.error)(f"{nombre}: {r.resultado} ({r.segundos} s)")

    def cierres(self, sesion: Sesion) -> None:
        filas = self._f.cierres.de_actor(ui.pedir_entero("ID del cliente/operador", minimo=1))
        ui.tabla(["Período", "Saldo inicial", "Débitos", "Créditos", "Saldo final", "Cerrado (UTC)"],
                 [(c["periodo"], ui.dinero(c["saldo_inicial"]), ui.dinero(c["total_debitos"]),
                   ui.dinero(c["total_creditos"]), ui.dinero(c["saldo_final"]), ui.fecha(c["fecha_cierre"]))
                  for c in filas], "Cierres mensuales (PostgreSQL, tabla cierre_mensual)")


def _intervalo(t) -> str:
    s = int(t.total_seconds())
    if s % 86400 == 0:
        return f"{s // 86400} día(s)"
    if s % 3600 == 0:
        return f"{s // 3600} h"
    if s % 60 == 0:
        return f"{s // 60} min"
    return f"{s} s"
