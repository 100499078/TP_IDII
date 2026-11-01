"""Menú de sensores y telemetría (IoT): consultas a Cassandra, Redis y MongoDB."""
from __future__ import annotations

from datetime import datetime, timezone

from app.dominio.modelos import Sesion
from app.presentacion import componentes as ui
from app.presentacion.menus.base import MenuBase
from app.presentacion.menus.envios import cliente_de


def _hoy() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _num(v, decimales=1) -> str:
    return "—" if v is None else f"{v:.{decimales}f}"


class MenuIoT(MenuBase):
    TITULO = "Sensores y telemetría (IoT)"
    OPCIONES = [
        ("1", "Última lectura de cada sensor", "ultimas", 2),
        ("2", "Lecturas de un sensor en un día", "lecturas_sensor", 2),
        ("3", "Lecturas de un contenedor en un día", "lecturas_contenedor", 2),
        ("4", "Recorrido GPS de un envío", "recorrido", 1),
        ("5", "Anomalías de temperatura de un día", "anomalias", 2),
        ("6", "Sensores sin contacto", "sin_contacto", 3),
        ("7", "Alertas abiertas", "alertas", 3),
        ("8", "Cambiar estado de una alerta", "estado_alerta", 3),
        ("9", "Registrar una lectura a mano", "lectura_manual", 3),
        ("10", "Generar lecturas simuladas (lote)", "lote", 4),
        # Fase 9: ABM de las reglas de alerta (están en PostgreSQL)
        ("11", "Reglas de alerta", "reglas", 2),
        ("12", "Modificar una regla de alerta", "modificar_regla", 4),
    ]

    def __init__(self, ejecutar, sesion_actual, servicio_telemetria, servicio_envios, simulador):
        super().__init__(ejecutar, sesion_actual)
        self._srv = servicio_telemetria
        self._envios = servicio_envios
        self._simulador = simulador

    def _pedir_dia(self):
        return ui.pedir_fecha_hora("Día", defecto=_hoy()).date()

    def ultimas(self, sesion: Sesion) -> None:
        id_contenedor = ui.pedir_entero("ID del contenedor (vacío = todos)", minimo=1, obligatorio=False)
        filas = []
        for f in self._srv.ultimas_lecturas(id_contenedor):
            s, l = f["sensor"], f["lectura"] or {}            # l vacío = el sensor nunca reportó
            rango = f"{s.temp_min:g} a {s.temp_max:g}" if s.temp_min is not None else "—"
            posicion = f"{l['latitud']:.3f}, {l['longitud']:.3f}" if l.get("latitud") is not None else "—"
            filas.append((s.id_sensor, s.tipo, s.codigo_contenedor,
                          ui.fecha(l["fecha_hora"]) if l else "sin lecturas", _num(l.get("temperatura")), rango,
                          _num(l.get("humedad"), 0), l.get("bateria", "—"), posicion))
        ui.tabla(["Sensor", "Tipo", "Contenedor", "Fecha (UTC)", "°C", "Rango °C", "Humedad %", "Batería %", "Posición"],
                 filas, "Última lectura por sensor (Cassandra, tabla ultima_lectura_por_sensor)")

    def lecturas_sensor(self, sesion: Sesion) -> None:
        id_sensor = ui.pedir_entero("ID del sensor", minimo=1)
        filas = self._srv.lecturas_sensor(id_sensor, self._pedir_dia())
        ui.tabla(["Fecha (UTC)", "°C", "Humedad %", "Vibración g", "Batería %", "Latitud", "Longitud"],
                 [(ui.fecha(f["fecha_hora"]), _num(f["temperatura"]), _num(f["humedad"], 0),
                   _num(f["vibracion"], 2), f["bateria"], _num(f["latitud"], 4), _num(f["longitud"], 4))
                  for f in filas], f"{len(filas)} lecturas, más recientes primero (Cassandra, lecturas_por_sensor)")

    def lecturas_contenedor(self, sesion: Sesion) -> None:
        id_contenedor = ui.pedir_entero("ID del contenedor", minimo=1)
        filas = self._srv.lecturas_contenedor(id_contenedor, self._pedir_dia())
        ui.tabla(["Fecha (UTC)", "Sensor", "°C", "Humedad %", "Vibración g", "Batería %"],
                 [(ui.fecha(f["fecha_hora"]), f["id_sensor"], _num(f["temperatura"]), _num(f["humedad"], 0),
                   _num(f["vibracion"], 2), f["bateria"]) for f in filas],
                 f"{len(filas)} lecturas (Cassandra, lecturas_por_contenedor)")

    def recorrido(self, sesion: Sesion) -> None:
        id_envio = ui.pedir_entero("ID del envío", minimo=1)
        envio = self._envios.obtener(id_envio, cliente_de(sesion))      # un cliente solo ve lo suyo
        filas = self._srv.recorrido_envio(id_envio)
        if len(filas) > 30:                                              # para no llenar la pantalla
            paso = len(filas) // 30 + 1
            filas = filas[::paso] + ([filas[-1]] if (len(filas) - 1) % paso else [])
        ui.tabla(["Fecha (UTC)", "Contenedor", "Latitud", "Longitud"],
                 [(ui.fecha(f["fecha_hora"]), f["id_contenedor"], _num(f["latitud"], 4), _num(f["longitud"], 4))
                  for f in filas], f"Recorrido del envío {id_envio}: {envio.origen} → {envio.destino} "
                                   "(Cassandra, posiciones_por_envio)")

    def anomalias(self, sesion: Sesion) -> None:
        filas = self._srv.anomalias(self._pedir_dia())
        ui.tabla(["Fecha (UTC)", "Sensor", "Contenedor", "Envío", "°C", "Rango permitido"],
                 [(ui.fecha(f["fecha_hora"]), f["id_sensor"], f["id_contenedor"], f["id_envio"] or "—",
                   _num(f["temperatura"]), f"{f['umbral_min']:g} a {f['umbral_max']:g}") for f in filas],
                 f"{len(filas)} anomalías (Cassandra, anomalias_temperatura)")

    def sin_contacto(self, sesion: Sesion) -> None:
        minutos = ui.pedir_entero("Sin contacto hace más de cuántos minutos", minimo=1, maximo=100_000)
        filas = self._srv.sensores_sin_contacto(minutos)
        ui.tabla(["Sensor", "Tipo", "Contenedor", "Último contacto (UTC)"],
                 [(f["sensor"].id_sensor, f["sensor"].tipo, f["sensor"].codigo_contenedor,
                   ui.fecha(f["ultimo"]) if f["ultimo"] else "nunca") for f in filas],
                 f"{len(filas)} sensores sin contacto (Redis, sensor:ultimo_contacto)")

    def alertas(self, sesion: Sesion) -> None:
        lista = self._srv.alertas()
        ui.tabla(["ID", "Fecha (UTC)", "Tipo", "Estado", "Envío", "Mensaje"],
                 [(a.id_alerta[-8:], ui.fecha(a.fecha), a.tipo, a.estado, a.id_envio or "—", a.mensaje)
                  for a in lista], f"{len(lista)} alertas NUEVAS o VISTAS (MongoDB, colección alertas)")
        if lista:
            ui.info("Para cambiar el estado alcanza con los 8 caracteres de la columna ID.")

    def estado_alerta(self, sesion: Sesion) -> None:
        corto = ui.pedir("ID de la alerta (8 caracteres o completo)").lower()
        candidatas = [a for a in self._srv.alertas() if a.id_alerta.endswith(corto)]
        if len(candidatas) != 1:
            ui.error("No hay una única alerta abierta con ese ID")
            return
        estado = ui.pedir_opcion("Nuevo estado", ["VISTA", "RESUELTA"])
        self._srv.cambiar_estado_alerta(candidatas[0].id_alerta, estado)
        ui.ok(f"Alerta {candidatas[0].tipo} → {estado}")

    def lectura_manual(self, sesion: Sesion) -> None:
        id_sensor = ui.pedir_entero("ID del sensor", minimo=1)
        temperatura = ui.pedir_decimal("Temperatura °C (vacío = no mide)", obligatorio=False)
        humedad = ui.pedir_decimal("Humedad % (vacío = no mide)", obligatorio=False)
        bateria = ui.pedir_entero("Batería %", minimo=0, maximo=100)
        alertas = self._srv.registrar_lectura(id_sensor, None, temperatura=temperatura, humedad=humedad,
                                              bateria=bateria)
        ui.ok("Lectura registrada en Cassandra")
        for a in alertas:
            ui.error(f"ALERTA {a.tipo}: {a.mensaje}")

    def lote(self, sesion: Sesion) -> None:
        horas = ui.pedir_entero("¿Cuántas horas hacia atrás?", minimo=1, maximo=168)
        cada = ui.pedir_entero("Una lectura cada cuántos minutos", minimo=1, maximo=120)
        with ui.consola.status("Generando lecturas..."):
            r = self._simulador.lote(horas, cada)
        ui.ok(f"{r.lecturas} lecturas registradas, {len(r.alertas)} alertas emitidas, {r.errores} rechazadas")

    # ------------------------------------------------------------------ reglas (fase 9)
    def reglas(self, sesion: Sesion) -> None:
        ui.tabla(["Regla", "Umbral", "Activa", "Descripción"],
                 [(r.tipo, "por sensor" if r.umbral is None else f"{r.umbral:g} {r.unidad}",
                   "sí" if r.activa else "[yellow]no[/yellow]", r.descripcion)
                  for r in self._srv.reglas().values()], "Reglas de alerta (PostgreSQL, tabla regla_alerta)")

    def modificar_regla(self, sesion: Sesion) -> None:
        self.reglas(sesion)
        reglas = self._srv.reglas()
        tipo = ui.pedir_opcion("Regla", list(reglas))
        regla = reglas[tipo]
        umbral = None
        if tipo != "TEMPERATURA":
            umbral = ui.pedir_decimal(f"Umbral en {regla.unidad}", minimo=0.1, defecto=f"{regla.umbral:g}")
        activa = ui.confirmar("¿Queda activa?", defecto=regla.activa)
        self._srv.modificar_regla(tipo, float(umbral) if umbral is not None else None, activa)
        ui.ok(f"Regla {tipo} actualizada (las otras terminales la toman en menos de un minuto)")
