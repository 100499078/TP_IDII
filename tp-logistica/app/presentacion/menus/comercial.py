"""Menú comercial: facturas, pagos y cuenta corriente."""
from __future__ import annotations

from rich.markup import escape

from app.dominio.modelos import MEDIOS_PAGO, Sesion
from app.presentacion import componentes as ui
from app.presentacion.menus.base import MenuBase


class MenuComercial(MenuBase):
    TITULO = "Facturación y cuenta corriente"
    OPCIONES = [
        ("1", "Facturas (mías o de un cliente/operador)", "facturas", 1),
        ("2", "Detalle de una factura", "detalle", 1),
        ("3", "Estado de cuenta corriente", "estado_cuenta", 1),
        ("4", "Facturar los envíos entregados de un cliente", "facturar_cliente", 4),
        ("5", "Facturación masiva (todos los clientes)", "facturar_todos", 4),
        ("6", "Registrar un pago", "pago", 4),
        ("7", "Anular una factura", "anular", 4),
        ("8", "Cargar factura de un operador o proveedor", "recibida", 4),
        ("9", "Deudores por antigüedad", "antiguedad", 4),
        ("10", "Marcar facturas vencidas", "vencidas", 4),
    ]

    def __init__(self, ejecutar, sesion_actual, servicio_comercial):
        super().__init__(ejecutar, sesion_actual)
        self._srv = servicio_comercial

    def _actor(self, sesion: Sesion) -> int | None:
        """Un usuario de un cliente ve solo lo suyo; el resto elige el actor."""
        if sesion.id_actor is not None:
            return sesion.id_actor
        return ui.pedir_entero("ID del cliente/operador (vacío = todos)", minimo=1, obligatorio=False)

    def facturas(self, sesion: Sesion) -> None:
        id_actor = self._actor(sesion)
        solo_abiertas = ui.confirmar("¿Solo las que están sin pagar?", defecto=False)
        lista = self._srv.facturas(id_actor, solo_abiertas)
        ui.tabla(["ID", "Número", "Tipo", "Actor", "Emisión", "Vence", "Total", "Pendiente", "Estado"],
                 [(f.id_factura, f.numero, f.tipo, escape(f.razon_social or ""), ui.fecha(f.fecha_emision),
                   ui.fecha(f.fecha_vencimiento), ui.dinero(f.importe_total),
                   ui.dinero(f.pendiente) if f.estado != "ANULADA" else "—", f.estado) for f in lista],
                 f"{len(lista)} facturas (PostgreSQL)")

    def detalle(self, sesion: Sesion) -> None:
        f = self._srv.factura(ui.pedir_entero("ID de la factura", minimo=1), sesion.id_actor)
        ui.tabla(["Dato", "Valor"], [
            ("Número", f"{f.numero} ({f.tipo})"), ("Actor", escape(f.razon_social or "")),
            ("Emisión / vencimiento", f"{ui.fecha(f.fecha_emision)} / {ui.fecha(f.fecha_vencimiento)}"),
            ("Total", ui.dinero(f.importe_total)), ("Pagado", ui.dinero(f.pagado)),
            ("Pendiente", ui.dinero(f.pendiente)), ("Estado", f.estado)], f"Factura {f.id_factura}")
        ui.tabla(["Descripción", "Envío", "Cantidad", "Precio", "Subtotal"],
                 [(escape(i.descripcion), i.id_envio or "—", i.cantidad, ui.dinero(i.precio_unitario),
                   ui.dinero(i.subtotal)) for i in f.items], "Ítems")
        if f.pagos:
            ui.tabla(["Pago", "Fecha (UTC)", "Medio", "Monto"],
                     [(p.id_pago, ui.fecha(p.fecha), p.medio_pago, ui.dinero(p.monto)) for p in f.pagos], "Pagos")

    def estado_cuenta(self, sesion: Sesion) -> None:
        id_actor = sesion.id_actor or ui.pedir_entero("ID del cliente/operador", minimo=1)
        c = self._srv.estado_de_cuenta(id_actor)
        ui.tabla(["Fecha (UTC)", "Movimiento", "Debe", "Haber", "Saldo"],
                 [(ui.fecha(m["fecha"]), escape(m["descripcion"] or ""),
                   ui.dinero(m["importe"]) if m["tipo"] == "DEBITO" else "",
                   ui.dinero(m["importe"]) if m["tipo"] == "CREDITO" else "", ui.dinero(m["saldo"]))
                  for m in c["movimientos"]], "Últimos movimientos (saldo acumulado con SUM() OVER)")
        limite = f" · límite de crédito {ui.dinero(c['limite'])}" if c["limite"] else ""
        ui.info(f"Saldo actual {ui.dinero(c['saldo'])} (positivo = nos debe){limite}")

    def facturar_cliente(self, sesion: Sesion) -> None:
        id_cliente = ui.pedir_entero("ID del cliente", minimo=1)
        envios = self._srv.envios_facturables(id_cliente)
        ui.tabla(["Envío", "Recorrido", "Entregado", "Costo de la ruta"],
                 [(e["id_envio"], f"{e['origen']} → {e['destino']}", ui.fecha(e["fecha_entrega_real"]),
                   ui.dinero(e["costo"])) for e in envios], "Envíos entregados sin facturar")
        reportes = self._srv.reportes_facturables(id_cliente)                     # fase 9
        if reportes:
            ui.tabla(["Reporte", "Pedido"], [(r["tipo"], ui.fecha(r["fecha_solicitud"])) for r in reportes],
                     "Reportes pedidos para el cliente, sin cobrar (MongoDB)")
        if (envios or reportes) and ui.confirmar("¿Emitir la factura (con IVA 21 %)?", defecto=True):
            f = self._srv.facturar_cliente(id_cliente)
            ui.ok(f"Factura {f.numero} por {ui.dinero(f.importe_total)}, vence el {ui.fecha(f.fecha_vencimiento)}")

    def facturar_todos(self, sesion: Sesion) -> None:
        if not ui.confirmar("Se factura a todos los clientes con envíos entregados. ¿Continuar?"):
            return
        with ui.consola.status("Facturando..."):
            emitidas = self._srv.facturar_todos()
        ui.tabla(["Número", "Cliente", "Total"],
                 [(f.numero, f.id_actor, ui.dinero(f.importe_total)) for f in emitidas],
                 f"{len(emitidas)} facturas emitidas")

    def pago(self, sesion: Sesion) -> None:
        f = self._srv.factura(ui.pedir_entero("ID de la factura", minimo=1))
        ui.info(f"{f.numero} de {f.razon_social}: total {ui.dinero(f.importe_total)}, falta {ui.dinero(f.pendiente)}")
        monto = ui.pedir("Monto", defecto=str(f.pendiente))
        medio = ui.pedir_opcion("Medio", list(MEDIOS_PAGO), defecto="TRANSFERENCIA")
        ui.ok(f"Pago registrado: la factura queda {self._srv.registrar_pago(f.id_factura, monto, medio)}")

    def anular(self, sesion: Sesion) -> None:
        id_factura = ui.pedir_entero("ID de la factura", minimo=1)
        if ui.confirmar("¿Anular la factura? Se registra el contraasiento en la cuenta"):
            self._srv.anular(id_factura)
            ui.ok("Factura anulada; sus envíos se pueden volver a facturar")

    def recibida(self, sesion: Sesion) -> None:
        id_actor = ui.pedir_entero("ID del operador o proveedor", minimo=1)
        numero = ui.pedir("Número de la factura (ej. B-0003-00001234)")
        importe = ui.pedir("Importe total")
        dias = ui.pedir_entero("Vence en cuántos días", minimo=0, maximo=180)
        descripcion = ui.pedir("Concepto", obligatorio=False)
        ui.ok(f"Factura cargada con ID {self._srv.registrar_factura_recibida(id_actor, numero, importe, dias, descripcion or '')}")

    def antiguedad(self, sesion: Sesion) -> None:
        filas = self._srv.antiguedad_deuda()
        ui.tabla(["Cliente", "A vencer", "1-30 días", "31-60 días", "Más de 60", "Total"],
                 [(escape(f["razon_social"]), ui.dinero(f["a_vencer"] or 0), ui.dinero(f["d1_30"] or 0),
                   ui.dinero(f["d31_60"] or 0), ui.dinero(f["mas_60"] or 0), ui.dinero(f["total"])) for f in filas],
                 "Deuda por antigüedad del vencimiento (SQL con FILTER)")

    def vencidas(self, sesion: Sesion) -> None:
        ui.ok(f"{self._srv.marcar_vencidas()} facturas pasaron a VENCIDA")
