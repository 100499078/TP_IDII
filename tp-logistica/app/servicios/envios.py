"""Registro de envíos y seguimiento de trayectos (tramos).

Reglas principales:
- Un envío nace con al menos un contenedor disponible y con volumen que entra en ellos.
- La ruta se arma tramo a tramo y tiene que ser continua: cada tramo sale de donde
  terminó el anterior (el primero, del origen del envío).
- El operador del tramo tiene que trabajar con ese medio de transporte, y el
  vehículo (si se indica) tiene que ser suyo y del tipo correcto.
- El costo del tramo se calcula con la tarifa vigente del medio: base + precio por kg.
- El estado sigue una máquina de estados (TRANSICIONES_ENVIO); ENTREGADO exige que
  la ruta llegue al destino con la llegada del último tramo registrada, y libera
  los contenedores.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable

from app.dominio.modelos import (ESTADOS_ENVIO_EN_CURSO, MEDIO_DE_VEHICULO, MEDIOS, PRIORIDADES,
                                 TRANSICIONES_ENVIO, Envio, Tramo)
from app.excepciones import EntidadNoEncontrada, ErrorConflicto, ErrorValidacion

log = logging.getLogger(__name__)


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


class ServicioEnvios:
    def __init__(self, repo_envios, repo_actores, repo_catalogos, repo_contenedores, repo_grafo=None,
                 reloj: Callable[[], datetime] = _ahora, control_credito: Callable[[int], None] | None = None):
        self._envios = repo_envios
        self._actores = repo_actores
        self._catalogos = repo_catalogos
        self._contenedores = repo_contenedores
        self._grafo = repo_grafo
        self._reloj = reloj
        self._control_credito = control_credito      # fase 6: ServicioComercial.controlar_credito

    # ------------------------------------------------------------------ envíos
    def crear_envio(self, id_cliente: int, origen: str, destino: str, fecha_entrega_estimada: datetime,
                    peso_kg: float, volumen_m3: float, contenedores: list[int], prioridad: str = "MEDIA") -> int:
        cliente = self._actores.obtener(id_cliente)
        if cliente is None or cliente.tipo != "CLIENTE":
            raise EntidadNoEncontrada(f"No existe el cliente {id_cliente}")
        if cliente.estado != "ACTIVO":
            raise ErrorConflicto("El cliente no está activo")
        if self._control_credito is not None:
            self._control_credito(id_cliente)          # si debe su límite de crédito, no puede pedir más
        origen, destino = origen.strip().upper(), destino.strip().upper()
        for codigo in (origen, destino):
            if self._catalogos.ubicacion(codigo) is None:
                raise EntidadNoEncontrada(f"No existe la ubicación {codigo}")
        if origen == destino:
            raise ErrorValidacion("El origen y el destino deben ser distintos")
        if fecha_entrega_estimada <= self._reloj():
            raise ErrorValidacion("La fecha de entrega estimada debe ser futura")
        if peso_kg <= 0 or volumen_m3 <= 0:
            raise ErrorValidacion("El peso y el volumen deben ser mayores que cero")
        if prioridad not in PRIORIDADES:
            raise ErrorValidacion(f"Prioridad inválida; opciones: {', '.join(PRIORIDADES)}")
        if not contenedores:
            raise ErrorValidacion("Asigná al menos un contenedor")
        capacidad = 0.0
        for id_contenedor in set(contenedores):
            c = self._contenedores.contenedor(id_contenedor)
            if c is None:
                raise EntidadNoEncontrada(f"No existe el contenedor {id_contenedor}")
            if c.estado != "DISPONIBLE":
                raise ErrorConflicto(f"El contenedor {c.codigo_internacional} está {c.estado}")
            capacidad += c.capacidad_m3
        if volumen_m3 > capacidad:
            raise ErrorValidacion(f"El volumen ({volumen_m3} m³) supera la capacidad de los contenedores ({capacidad} m³)")

        envio = Envio(None, id_cliente, origen, destino, fecha_entrega_estimada, peso_kg, volumen_m3,
                      prioridad=prioridad, fecha_creacion=self._reloj(),     # el reloj del servicio (fase 8)
                      contenedores=sorted(set(contenedores)))
        id_envio = self._envios.crear(envio)
        log.info("Envío %s creado para el cliente %s (%s → %s)", id_envio, id_cliente, origen, destino)
        return id_envio

    def obtener(self, id_envio: int, id_cliente: int | None = None, id_operador: int | None = None) -> Envio:
        """id_cliente: si se indica, el envío tiene que ser de ese cliente (usuarios con rol CLIENTE).
        id_operador (fase 9): el operador tiene que mover al menos un tramo (usuarios con rol SOCIO)."""
        envio = self._envios.obtener(id_envio)
        if envio is None or (id_cliente is not None and envio.id_cliente != id_cliente) or (
                id_operador is not None and all(t.id_operador != id_operador for t in envio.tramos)):
            raise EntidadNoEncontrada(f"No existe el envío {id_envio}")
        return envio

    def listar(self, estado: str | None = None, id_cliente: int | None = None,
               id_operador: int | None = None) -> list[dict]:
        if id_operador is None:
            return self._envios.listar(estado, id_cliente)
        return self._envios.listar(estado, id_cliente, id_operador=id_operador)

    # ------------------------------------------------------------------ modificar (fase 9)
    def modificar_envio(self, id_envio: int, prioridad: str | None = None,
                        fecha_entrega_estimada: datetime | None = None) -> None:
        """Cambia la prioridad o la fecha comprometida de un envío que todavía no terminó."""
        envio = self.obtener(id_envio)
        if not envio.en_curso:
            raise ErrorConflicto(f"El envío está {envio.estado}: ya no se modifica")
        prioridad = prioridad or envio.prioridad
        fecha = fecha_entrega_estimada or envio.fecha_entrega_estimada
        if prioridad not in PRIORIDADES:
            raise ErrorValidacion(f"Prioridad inválida; opciones: {', '.join(PRIORIDADES)}")
        if fecha <= envio.fecha_creacion:
            raise ErrorValidacion("La entrega estimada tiene que ser posterior a la creación del envío")
        if prioridad == envio.prioridad and fecha == envio.fecha_entrega_estimada:
            raise ErrorValidacion("No hay nada para cambiar")
        self._envios.modificar(id_envio, prioridad, fecha, self._reloj())
        log.info("Envío %s modificado: prioridad %s, entrega estimada %s", id_envio, prioridad, fecha)

    def agregar_contenedor(self, id_envio: int, id_contenedor: int) -> None:
        envio = self._envio_sin_salir(id_envio)
        if id_contenedor in envio.contenedores:
            raise ErrorConflicto(f"El contenedor {id_contenedor} ya está en el envío")
        c = self._contenedores.contenedor(id_contenedor)
        if c is None:
            raise EntidadNoEncontrada(f"No existe el contenedor {id_contenedor}")
        if c.estado != "DISPONIBLE":
            raise ErrorConflicto(f"El contenedor {c.codigo_internacional} está {c.estado}")
        self._envios.agregar_contenedor(id_envio, id_contenedor, self._reloj())
        log.info("Contenedor %s agregado al envío %s", id_contenedor, id_envio)

    def quitar_contenedor(self, id_envio: int, id_contenedor: int) -> None:
        envio = self._envio_sin_salir(id_envio)
        if id_contenedor not in envio.contenedores:
            raise EntidadNoEncontrada(f"El contenedor {id_contenedor} no está en el envío {id_envio}")
        if len(envio.contenedores) == 1:
            raise ErrorConflicto("Un envío necesita al menos un contenedor: agregá otro antes de quitar este")
        capacidad = sum(self._contenedores.contenedor(k).capacidad_m3 for k in envio.contenedores if k != id_contenedor)
        if envio.volumen_m3 > capacidad:
            raise ErrorConflicto(f"Sin ese contenedor no entra la carga ({envio.volumen_m3} m³ en {capacidad} m³)")
        self._envios.quitar_contenedor(id_envio, id_contenedor)
        log.info("Contenedor %s quitado del envío %s (vuelve a DISPONIBLE)", id_contenedor, id_envio)

    def _envio_sin_salir(self, id_envio: int) -> Envio:
        envio = self.obtener(id_envio)
        if envio.estado != "CREADO":
            raise ErrorConflicto(f"El envío está {envio.estado}: los contenedores se cambian antes de que salga")
        return envio

    # ------------------------------------------------------------------ tramos
    def agregar_tramo(self, id_envio: int, medio: str, destino: str, id_operador: int,
                      fecha_salida: datetime, fecha_llegada_estimada: datetime,
                      id_vehiculo: int | None = None) -> int:
        envio = self.obtener(id_envio)
        if envio.estado not in ESTADOS_ENVIO_EN_CURSO:
            raise ErrorConflicto(f"El envío está {envio.estado}: no admite tramos nuevos")
        if medio not in MEDIOS:
            raise ErrorValidacion(f"Medio inválido; opciones: {', '.join(MEDIOS)}")
        origen = envio.ultimo_destino            # continuidad de la ruta
        destino = destino.strip().upper()
        if self._catalogos.ubicacion(destino) is None:
            raise EntidadNoEncontrada(f"No existe la ubicación {destino}")
        if destino == origen:
            raise ErrorValidacion(f"El tramo sale de {origen}: el destino tiene que ser otro")

        operador = self._actores.obtener(id_operador)
        if operador is None or operador.tipo != "OPERADOR":
            raise EntidadNoEncontrada(f"No existe el operador {id_operador}")
        if operador.estado != "ACTIVO":
            raise ErrorConflicto("El operador no está activo")
        if medio not in operador.medios:
            raise ErrorValidacion(f"{operador.razon_social} no trabaja con transporte {medio.lower()}")
        if id_vehiculo is not None:
            vehiculo = self._catalogos.vehiculo(id_vehiculo)
            if vehiculo is None:
                raise EntidadNoEncontrada(f"No existe el vehículo {id_vehiculo}")
            if vehiculo.id_operador != id_operador:
                raise ErrorValidacion("El vehículo no pertenece a ese operador")
            if MEDIO_DE_VEHICULO[vehiculo.tipo] != medio:
                raise ErrorValidacion(f"Un {vehiculo.tipo.lower()} no hace transporte {medio.lower()}")
            if vehiculo.estado != "OPERATIVO":
                raise ErrorConflicto(f"El vehículo está en {vehiculo.estado.lower()}")

        if fecha_llegada_estimada <= fecha_salida:
            raise ErrorValidacion("La llegada estimada debe ser posterior a la salida")
        if envio.tramos and fecha_salida < envio.tramos[-1].fecha_llegada_estimada:
            raise ErrorValidacion("El tramo no puede salir antes de la llegada estimada del tramo anterior")
        if fecha_salida < envio.fecha_creacion:
            raise ErrorValidacion("El tramo no puede salir antes de la creación del envío")

        tarifa = self._catalogos.tarifa_vigente(medio, fecha_salida.date())
        if tarifa is None:
            raise ErrorConflicto(f"No hay una tarifa vigente para transporte {medio.lower()}")
        tramo = Tramo(None, id_envio, 0, medio, origen, destino, id_operador, fecha_salida,
                      fecha_llegada_estimada, id_vehiculo=id_vehiculo, costo=tarifa.costo(envio.peso_kg))
        id_tramo = self._envios.agregar_tramo(tramo)
        log.info("Tramo %s agregado al envío %s: %s → %s (%s)", id_tramo, id_envio, origen, destino, medio)
        return id_tramo

    def registrar_llegada(self, id_tramo: int, fecha: datetime | None = None) -> None:
        tramo = self._envios.tramo(id_tramo)
        if tramo is None:
            raise EntidadNoEncontrada(f"No existe el tramo {id_tramo}")
        if tramo.fecha_llegada_real is not None:
            raise ErrorConflicto("La llegada de ese tramo ya está registrada")
        fecha = fecha or self._reloj()
        if fecha < tramo.fecha_salida:
            raise ErrorValidacion("La llegada no puede ser anterior a la salida")
        self._envios.registrar_llegada(tramo, fecha)

    # ------------------------------------------------------------------ estados
    def cambiar_estado(self, id_envio: int, nuevo: str) -> list[int]:
        envio = self.obtener(id_envio)
        permitidos = TRANSICIONES_ENVIO.get(envio.estado, ())
        if nuevo not in permitidos:
            raise ErrorConflicto(f"Un envío {envio.estado} no puede pasar a {nuevo}. "
                                 f"Opciones: {', '.join(permitidos) or 'ninguna (estado final)'}")
        if nuevo == "EN_TRANSITO" and not envio.tramos:
            raise ErrorConflicto("Cargá al menos un tramo antes de poner el envío en tránsito")
        fecha = self._reloj()
        if nuevo == "ENTREGADO":
            if envio.ultimo_destino != envio.destino:
                raise ErrorConflicto(f"La ruta termina en {envio.ultimo_destino}, no en el destino {envio.destino}")
            ultimo = envio.tramos[-1]
            if ultimo.fecha_llegada_real is None:
                raise ErrorConflicto(f"Registrá primero la llegada del último tramo ({ultimo.id_tramo})")
            fecha = ultimo.fecha_llegada_real        # la entrega real es la llegada al destino
        termina = nuevo in ("ENTREGADO", "CANCELADO")
        liberados = self._envios.cambiar_estado(id_envio, nuevo, fecha, liberar_contenedores=termina)
        log.info("Envío %s: %s → %s", id_envio, envio.estado, nuevo)
        return liberados

    # ------------------------------------------------------------------ grafo
    def ruta_en_grafo(self, id_envio: int) -> list[dict]:
        """La misma ruta leída desde Neo4j, para comprobar que la sincronización funciona."""
        if self._grafo is None:
            return []
        return self._grafo.ruta_envio(id_envio)
