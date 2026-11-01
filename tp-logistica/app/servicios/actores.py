"""Alta, baja y modificación de clientes, operadores logísticos y proveedores."""
from __future__ import annotations

import logging

from app.dominio.modelos import MEDIOS, TIPOS_ACTOR, Actor
from app.dominio.validaciones import validar_cuit, validar_email, validar_pais
from app.excepciones import EntidadNoEncontrada, ErrorConflicto, ErrorValidacion

log = logging.getLogger(__name__)


class ServicioActores:
    def __init__(self, repo_actores):
        self._repo = repo_actores

    def _validar(self, a: Actor) -> None:
        if a.tipo not in TIPOS_ACTOR:
            raise ErrorValidacion(f"Tipo inválido: {a.tipo}")
        a.razon_social = (a.razon_social or "").strip()
        if len(a.razon_social) < 3:
            raise ErrorValidacion("La razón social debe tener al menos 3 caracteres")
        a.email = (a.email or "").strip().lower()
        a.pais = (a.pais or "").strip().upper()
        for problema in (validar_cuit(a.cuit), validar_email(a.email), validar_pais(a.pais)):
            if problema:
                raise ErrorValidacion(problema)
        if a.tipo == "CLIENTE":
            if a.limite_credito is not None and a.limite_credito < 0:
                raise ErrorValidacion("El límite de crédito no puede ser negativo")
            if a.condicion_pago_dias is not None and not 0 <= a.condicion_pago_dias <= 180:
                raise ErrorValidacion("La condición de pago debe estar entre 0 y 180 días")
        if a.tipo == "OPERADOR":
            if not a.medios or any(m not in MEDIOS for m in a.medios):
                raise ErrorValidacion(f"Indicá al menos un medio de transporte entre {', '.join(MEDIOS)}")
        if a.tipo == "PROVEEDOR" and not (a.rubro or "").strip():
            raise ErrorValidacion("Indicá el rubro del proveedor")

    def crear(self, actor: Actor) -> int:
        actor.id_actor = None
        actor.estado = "ACTIVO"
        self._validar(actor)
        id_actor = self._repo.crear(actor)
        log.info("Alta de %s %s (%s)", actor.tipo.lower(), id_actor, actor.razon_social)
        return id_actor

    def obtener(self, id_actor: int, tipo: str | None = None) -> Actor:
        actor = self._repo.obtener(id_actor)
        if actor is None or (tipo and actor.tipo != tipo):
            raise EntidadNoEncontrada(f"No existe el {(tipo or 'actor').lower()} {id_actor}")
        return actor

    def modificar(self, actor: Actor) -> None:
        actual = self.obtener(actor.id_actor)
        actor.tipo, actor.cuit, actor.estado = actual.tipo, actual.cuit, actual.estado   # no se cambian por acá
        self._validar(actor)
        self._repo.actualizar(actor)
        log.info("Modificación del actor %s", actor.id_actor)

    def dar_de_baja(self, id_actor: int) -> None:
        """Baja LÓGICA: el actor pasa a INACTIVO y sus datos e historial se conservan."""
        actor = self.obtener(id_actor)
        if actor.estado == "INACTIVO":
            raise ErrorConflicto("El actor ya está dado de baja")
        if actor.tipo == "CLIENTE" and self._repo.envios_en_curso_de_cliente(id_actor):
            raise ErrorConflicto("El cliente tiene envíos en curso: no se puede dar de baja")
        if actor.tipo == "OPERADOR" and self._repo.tramos_en_curso_de_operador(id_actor):
            raise ErrorConflicto("El operador tiene tramos de envíos en curso: no se puede dar de baja")
        actor.estado = "INACTIVO"
        self._repo.actualizar(actor)
        log.info("Baja del actor %s", id_actor)

    def reactivar(self, id_actor: int) -> None:
        actor = self.obtener(id_actor)
        actor.estado = "ACTIVO"
        self._repo.actualizar(actor)

    def listar(self, tipo: str | None = None, solo_activos: bool = False, texto: str | None = None) -> list[Actor]:
        return self._repo.listar(tipo, solo_activos, texto)
