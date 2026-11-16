"""Gestión comercial (fase 6): facturación, pagos, cuenta corriente y control de crédito.

- Facturar: los envíos ENTREGADOS de un cliente que no estén facturados; un ítem por envío
  con el costo de su ruta (suma de los tramos) y un ítem de IVA (21 %). Vence según la
  condición de pago del cliente.
- Facturación masiva: todos los clientes, protegida con un lock en Redis para que dos
  personas no la corran a la vez.
- Pagos parciales o totales; la factura pasa a PARCIAL o PAGADA.
- Anulación: solo sin pagos; genera el contraasiento.
- Un cliente (rol CLIENTE) solo ve sus facturas y su cuenta.
- Dinero siempre como Decimal.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable

from app.dominio.modelos import IVA, MEDIOS_PAGO, PRECIO_REPORTE, Factura, ItemFactura, Sesion, pesos
from app.excepciones import EntidadNoEncontrada, ErrorConflicto, ErrorValidacion

log = logging.getLogger(__name__)

LOCK_FACTURACION = "facturacion"
LOCK_SEGUNDOS = 600


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def a_pesos(valor) -> Decimal:
    try:
        return pesos(str(valor).replace(",", "."))
    except (InvalidOperation, ValueError) as exc:
        raise ErrorValidacion("El importe no es un número válido") from exc


class ServicioComercial:
    def __init__(self, repo_comercial, repo_actores, repo_locks=None, reloj: Callable[[], datetime] = _ahora,
                 repo_reportes=None):
        self._repo = repo_comercial
        self._actores = repo_actores
        self._locks = repo_locks
        self._reloj = reloj
        self._reportes = repo_reportes      # fase 9: reportes facturables (MongoDB); None = no se cobran

    def _hoy(self) -> date:
        return self._reloj().date()

    def _cliente(self, id_cliente: int):
        c = self._actores.obtener(id_cliente)
        if c is None or c.tipo != "CLIENTE":
            raise EntidadNoEncontrada(f"No existe el cliente {id_cliente}")
        return c

    # ------------------------------------------------------------------ facturar
    def envios_facturables(self, id_cliente: int) -> list[dict]:
        self._cliente(id_cliente)
        return self._repo.envios_facturables(id_cliente)

    def reportes_facturables(self, id_cliente: int) -> list[dict]:
        """Fase 9: los reportes GENERADOS para el cliente que todavía no se cobraron.
        Lo que MongoDB todavía marca como "sin cobrar" pero PostgreSQL ya tiene en una factura (porque la
        marca falló después del COMMIT) se reconcilia acá: se marca y no se vuelve a cobrar."""
        if self._reportes is None:
            return []
        candidatos = self._reportes.facturables(id_cliente)
        ya = self._repo.reportes_ya_facturados([r["id_reporte"] for r in candidatos])
        for id_reporte, f in ya.items():
            self._reportes.marcar_facturados([id_reporte], f["id_factura"], f["numero"])
        return [r for r in candidatos if r["id_reporte"] not in ya]

    def facturar_cliente(self, id_cliente: int) -> Factura | None:
        """Una factura con todos los envíos entregados sin facturar (y, desde la fase 9, los reportes
        pedidos para el cliente). None si no hay nada que facturar."""
        cliente = self._cliente(id_cliente)
        envios = self._repo.envios_facturables(id_cliente)
        reportes = self.reportes_facturables(id_cliente)
        if not envios and not reportes:
            return None
        items = [ItemFactura(f"Envío {e['id_envio']}: {e['origen']} → {e['destino']}", Decimal("1"),
                             pesos(e["costo"]), id_envio=e["id_envio"]) for e in envios]
        from app.servicios.reportes import TIPOS_REPORTE
        items += [ItemFactura(f"Reporte {TIPOS_REPORTE.get(r['tipo'], r['tipo'])} ({r['fecha_solicitud']:%d/%m/%Y})",
                              Decimal("1"), PRECIO_REPORTE, id_reporte=r["id_reporte"]) for r in reportes]
        neto = pesos(sum((i.subtotal for i in items), Decimal("0")))
        items.append(ItemFactura("IVA 21 %", Decimal("1"), pesos(neto * IVA)))
        hoy = self._hoy()
        vencimiento = hoy + timedelta(days=cliente.condicion_pago_dias or 0)
        factura = self._repo.emitir(id_cliente, items, hoy, vencimiento)
        if reportes:
            # Después del COMMIT de PostgreSQL se marca en MongoDB. Si esto falla, el índice único de
            # item_factura.id_reporte impide cobrarlo de nuevo y la próxima facturación lo reconcilia.
            try:
                self._reportes.marcar_facturados([r["id_reporte"] for r in reportes], factura.id_factura,
                                                 factura.numero)
            except Exception as exc:  # noqa: BLE001
                log.warning("Factura %s emitida, pero no se pudieron marcar sus reportes en MongoDB: %s",
                            factura.numero, exc)
        log.info("Factura %s a %s por $ %s (%d envíos)", factura.numero, cliente.razon_social, factura.importe_total,
                 len(envios))
        return factura

    def facturar_todos(self) -> list[Factura]:
        """Facturación masiva. El lock de Redis evita que dos operadores la corran a la vez
        (el índice único de PostgreSQL igual impediría facturar dos veces, pero el segundo
        proceso fallaría a mitad de camino)."""
        token = None
        if self._locks is not None:
            token = self._locks.tomar(LOCK_FACTURACION, LOCK_SEGUNDOS)
            if token is None:
                raise ErrorConflicto("Otra persona está corriendo la facturación en este momento; probá en unos minutos")
        try:
            emitidas = []
            clientes = set(self._repo.clientes_con_envios_facturables())
            if self._reportes is not None:
                clientes |= set(self._reportes.clientes_con_facturables())
            for id_cliente in sorted(clientes):
                try:
                    factura = self.facturar_cliente(id_cliente)
                    if factura:
                        emitidas.append(factura)
                except ErrorConflicto as exc:              # uno que falla no frena a los demás
                    log.warning("No se pudo facturar al cliente %s: %s", id_cliente, exc)
            return emitidas
        finally:
            if token:
                self._locks.soltar(LOCK_FACTURACION, token)

    def registrar_factura_recibida(self, id_actor: int, numero: str, importe, dias_vencimiento: int,
                                   descripcion: str) -> int:
        actor = self._actores.obtener(id_actor)
        if actor is None or actor.tipo not in ("OPERADOR", "PROVEEDOR"):
            raise EntidadNoEncontrada(f"No existe el operador o proveedor {id_actor}")
        numero = (numero or "").strip().upper()
        if not 3 <= len(numero) <= 20:
            raise ErrorValidacion("El número de factura tiene entre 3 y 20 caracteres")
        monto = a_pesos(importe)
        if monto <= 0:
            raise ErrorValidacion("El importe tiene que ser mayor que cero")
        if not 0 <= dias_vencimiento <= 180:
            raise ErrorValidacion("El vencimiento va de 0 a 180 días")
        hoy = self._hoy()
        return self._repo.registrar_recibida(id_actor, numero, hoy, hoy + timedelta(days=dias_vencimiento), monto,
                                             descripcion.strip() or f"Factura {numero}")

    # ------------------------------------------------------------------ pagos y anulación
    def registrar_pago(self, id_factura: int, monto, medio: str) -> str:
        monto = a_pesos(monto)
        if monto <= 0:
            raise ErrorValidacion("El pago tiene que ser mayor que cero")
        if medio not in MEDIOS_PAGO:
            raise ErrorValidacion(f"Medio inválido; opciones: {', '.join(MEDIOS_PAGO)}")
        # "No pagar de más" depende de otros pagos que pueden llegar a la vez: lo controla el
        # repositorio DENTRO de la transacción, con la factura bloqueada (FOR UPDATE)
        estado = self._repo.registrar_pago(id_factura, monto, medio, self._reloj())
        log.info("Pago de $ %s a la factura %s (%s): queda %s", monto, id_factura, medio, estado)
        return estado

    def anular(self, id_factura: int) -> None:
        reportes = self._repo.anular(id_factura) or []
        if reportes and self._reportes is not None:
            try:
                self._reportes.desmarcar_facturados(reportes)     # vuelven a quedar para cobrar
            except Exception as exc:  # noqa: BLE001
                log.warning("No se pudieron desmarcar en MongoDB los reportes de la factura %s: %s", id_factura, exc)
        log.warning("Factura %s anulada", id_factura)

    def marcar_vencidas(self) -> int:
        return self._repo.marcar_vencidas(self._hoy())

    # ------------------------------------------------------------------ consultas
    def facturas(self, id_actor: int | None, solo_abiertas: bool = False) -> list[Factura]:
        return self._repo.facturas(id_actor, solo_abiertas)

    def factura(self, id_factura: int, id_actor_permitido: int | None = None) -> Factura:
        """id_actor_permitido: para un usuario de un cliente, la factura tiene que ser suya."""
        f = self._repo.factura(id_factura)
        if f is None or (id_actor_permitido is not None and f.id_actor != id_actor_permitido):
            raise EntidadNoEncontrada(f"No existe la factura {id_factura}")
        return f

    def estado_de_cuenta(self, id_actor: int) -> dict:
        cuenta = self._repo.cuenta(id_actor)
        if cuenta is None:
            raise EntidadNoEncontrada(f"El actor {id_actor} no tiene cuenta corriente")
        return {"saldo": cuenta["saldo"], "limite": cuenta["limite_credito"],
                "actualizada": cuenta["fecha_actualizacion"], "movimientos": self._repo.movimientos(id_actor)}

    def antiguedad_deuda(self) -> list[dict]:
        return self._repo.antiguedad_deuda(self._hoy())

    # ------------------------------------------------------------------ control de crédito
    def controlar_credito(self, id_cliente: int) -> None:
        """Lo usa la creación de envíos: un cliente que ya debe su límite de crédito no puede
        pedir envíos nuevos. Límite 0 = sin límite configurado."""
        cuenta = self._repo.cuenta(id_cliente)
        if cuenta is None or not cuenta["limite_credito"]:
            return
        if cuenta["saldo"] >= cuenta["limite_credito"]:
            raise ErrorConflicto(f"El cliente debe $ {cuenta['saldo']} y su límite de crédito es "
                                 f"$ {cuenta['limite_credito']}: regularizá los pagos antes de crear envíos")

    @staticmethod
    def actor_visible(sesion: Sesion, id_pedido: int | None) -> int | None:
        """Un usuario de un cliente solo ve lo suyo, pida lo que pida."""
        return sesion.id_actor if sesion.id_actor is not None else id_pedido
