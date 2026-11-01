"""Repositorios en memoria para probar los servicios sin bases de datos.

Implementan los mismos métodos que los repositorios reales. Como los servicios
reciben sus repositorios por constructor, en las pruebas se les pasan estos.
"""
from __future__ import annotations

from app.dominio.modelos import Rol, Sesion, Usuario


class UsuariosEnMemoria:
    def __init__(self):
        self.usuarios: dict[str, tuple[Usuario, str]] = {}

    def agregar(self, usuario: Usuario, password_hash: str) -> None:
        self.usuarios[usuario.email.lower()] = (usuario, password_hash)

    def buscar_por_email(self, email):
        return self.usuarios.get(email.lower())


class HistorialEnMemoria:
    def __init__(self):
        self.filas: dict[str, dict] = {}

    def registrar_inicio(self, id_sesion, id_usuario):
        self.filas[id_sesion] = {"id_sesion": id_sesion, "id_usuario": id_usuario, "estado": "ACTIVA"}

    def finalizar(self, id_sesion, estado):
        fila = self.filas.get(id_sesion)
        if fila is None or fila["estado"] != "ACTIVA":
            return False
        fila["estado"] = estado
        return True

    def activas(self):
        return [f for f in self.filas.values() if f["estado"] == "ACTIVA"]


class SesionesEnMemoria:
    def __init__(self, fallar_al_guardar: bool = False):
        self.sesiones: dict[str, Sesion] = {}
        self.ttl: dict[str, int] = {}
        self.fallar_al_guardar = fallar_al_guardar

    def guardar(self, sesion, ttl):
        if self.fallar_al_guardar:
            raise ConnectionError("Redis caído")
        self.sesiones[sesion.id_sesion] = sesion
        self.ttl[sesion.id_sesion] = ttl

    def obtener(self, id_sesion):
        return self.sesiones.get(id_sesion)

    def renovar(self, id_sesion, ttl, ahora):
        self.ttl[id_sesion] = ttl
        self.sesiones[id_sesion].ultimo_acceso = ahora

    def segundos_restantes(self, id_sesion):
        return self.ttl.get(id_sesion, -2)

    def eliminar(self, id_sesion, id_usuario):
        self.sesiones.pop(id_sesion, None)
        self.ttl.pop(id_sesion, None)

    def existe(self, id_sesion):
        return id_sesion in self.sesiones

    def vencer(self, id_sesion):
        """Simula que Redis borró la clave porque se cumplió el TTL."""
        self.eliminar(id_sesion, None)


def usuario_de_prueba(email="ana@demo.com", estado="ACTIVO", nivel=3) -> Usuario:
    return Usuario(id_usuario=7, nombre="Ana", apellido="Pérez", email=email, estado=estado,
                   roles=[Rol(id_rol=3, nombre="OPERADOR", nivel_permisos=nivel)])


# =====================================================================
# Fase 2 — gestión operativa
# =====================================================================
import copy  # noqa: E402
from datetime import date, datetime, timezone  # noqa: E402

from app.dominio.modelos import Actor, Contenedor, Envio, Tarifa, Tramo, Ubicacion, Vehiculo  # noqa: E402

AHORA = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


class ActoresEnMemoria:
    def __init__(self):
        self.actores: dict[int, Actor] = {}
        self.envios_en_curso: dict[int, int] = {}
        self.tramos_en_curso: dict[int, int] = {}

    def crear(self, a):
        a.id_actor = len(self.actores) + 1
        self.actores[a.id_actor] = copy.deepcopy(a)
        return a.id_actor

    def actualizar(self, a):
        self.actores[a.id_actor] = copy.deepcopy(a)

    def obtener(self, id_actor):
        a = self.actores.get(id_actor)
        return copy.deepcopy(a) if a else None

    def listar(self, tipo=None, solo_activos=False, texto=None):
        return [copy.deepcopy(a) for a in self.actores.values() if tipo in (None, a.tipo)]

    def envios_en_curso_de_cliente(self, id_actor):
        return self.envios_en_curso.get(id_actor, 0)

    def tramos_en_curso_de_operador(self, id_actor):
        return self.tramos_en_curso.get(id_actor, 0)


class CatalogosEnMemoria:
    def __init__(self):
        self.ubicaciones = {c: Ubicacion(i, c, c, "PUERTO", c[:2], 0, 0)
                            for i, c in enumerate(["ARBUE", "ARROS", "UYMVD", "NLRTM"], 1)}
        self.vehiculos: dict[int, Vehiculo] = {}
        self.tarifas = [Tarifa(1, "TERRESTRE", 1000, 2, date(2026, 1, 1)),
                        Tarifa(2, "MARITIMO", 5000, 1, date(2026, 1, 1))]

    def ubicacion(self, codigo):
        return self.ubicaciones.get(codigo)

    def listar_ubicaciones(self):
        return list(self.ubicaciones.values())

    def vehiculo(self, id_vehiculo):
        return self.vehiculos.get(id_vehiculo)

    def crear_vehiculo(self, v):
        v.id_vehiculo = len(self.vehiculos) + 1
        self.vehiculos[v.id_vehiculo] = v
        return v.id_vehiculo

    def listar_vehiculos(self, id_operador=None):
        return [v for v in self.vehiculos.values() if id_operador in (None, v.id_operador)]

    def tarifa_vigente(self, medio, dia):
        candidatas = [t for t in self.tarifas if t.medio_transporte == medio and t.vigente_desde <= dia]
        return max(candidatas, key=lambda t: t.vigente_desde, default=None)


class ContenedoresEnMemoria:
    def __init__(self):
        self.contenedores = {1: Contenedor(1, "MSCU1234566", "DRY", 30), 2: Contenedor(2, "MSCU2345672", "DRY", 30)}

    def contenedor(self, id_contenedor):
        c = self.contenedores.get(id_contenedor)
        return copy.deepcopy(c) if c else None

    def listar_contenedores(self, estado=None):
        return [c for c in self.contenedores.values() if estado in (None, c.estado)]


class EnviosEnMemoria:
    """Imita al repositorio real: asigna orden a los tramos y ocupa/libera contenedores."""

    def __init__(self, contenedores: ContenedoresEnMemoria):
        self.envios: dict[int, Envio] = {}
        self.tramos: dict[int, Tramo] = {}
        self._contenedores = contenedores

    def crear(self, e):
        e.id_envio, e.fecha_creacion = len(self.envios) + 1, AHORA
        self.envios[e.id_envio] = e
        for k in e.contenedores:
            self._contenedores.contenedores[k].estado = "EN_USO"
        return e.id_envio

    def agregar_tramo(self, t):
        e = self.envios[t.id_envio]
        t.orden, t.id_tramo = len(e.tramos) + 1, len(self.tramos) + 1
        e.tramos.append(t)
        self.tramos[t.id_tramo] = t
        return t.id_tramo

    def cambiar_estado(self, id_envio, estado, fecha, liberar_contenedores):
        e = self.envios[id_envio]
        e.estado = estado
        if not liberar_contenedores:
            return []
        for k in e.contenedores:
            self._contenedores.contenedores[k].estado = "DISPONIBLE"
        return list(e.contenedores)

    def registrar_llegada(self, t, fecha):
        self.tramos[t.id_tramo].fecha_llegada_real = fecha

    # fase 9
    def modificar(self, id_envio, prioridad, fecha_entrega_estimada, ahora):
        e = self.envios[id_envio]
        e.prioridad, e.fecha_entrega_estimada = prioridad, fecha_entrega_estimada

    def agregar_contenedor(self, id_envio, id_contenedor, ahora):
        self.envios[id_envio].contenedores.append(id_contenedor)
        self._contenedores.contenedores[id_contenedor].estado = "EN_USO"

    def quitar_contenedor(self, id_envio, id_contenedor):
        self.envios[id_envio].contenedores.remove(id_contenedor)
        self._contenedores.contenedores[id_contenedor].estado = "DISPONIBLE"

    def obtener(self, id_envio):
        e = self.envios.get(id_envio)
        return copy.deepcopy(e) if e else None

    def tramo(self, id_tramo):
        t = self.tramos.get(id_tramo)
        return copy.deepcopy(t) if t else None

    def listar(self, estado=None, id_cliente=None):
        return [e for e in self.envios.values() if id_cliente in (None, e.id_cliente)]


class OutboxEnMemoria:
    def __init__(self, eventos=None):
        self.eventos = [dict(e, publicado=False) for e in (eventos or [])]

    def pendientes(self, limite=200):
        return [e for e in self.eventos if not e["publicado"]][:limite]

    def marcar_publicados(self, ids):
        for e in self.eventos:
            if e["id_evento"] in ids:
                e["publicado"] = True

    def contar_pendientes(self):
        return len(self.pendientes())


class ColaEnMemoria:
    """Imita un Redis Stream con un grupo: pendientes (entregados sin XACK) y nuevos."""

    def __init__(self):
        self.mensajes: list[tuple[str, dict]] = []
        self.siguiente = 0               # posición del próximo mensaje nuevo (">")
        self.pendientes: dict[str, int] = {}   # id -> veces entregado
        self.fallidos: list[dict] = []

    def asegurar_grupo(self):
        pass

    def publicar(self, eventos):
        import json
        for e in eventos:
            self.mensajes.append((f"{len(self.mensajes) + 1}-0", {
                "id_evento": str(e["id_evento"]), "tipo_evento": e["tipo_evento"],
                "payload": json.dumps(e["payload"])}))

    def leer(self, consumidor, cantidad=100):
        if self.pendientes:            # primero lo ya entregado y sin confirmar ("0")
            ids = list(self.pendientes)[:cantidad]
        else:                          # después lo nuevo (">")
            ids = [i for i, _ in self.mensajes[self.siguiente:self.siguiente + cantidad]]
            self.siguiente += len(ids)
        for i in ids:
            self.pendientes[i] = self.pendientes.get(i, 0) + 1
        campos = dict(self.mensajes)
        return [(i, campos[i]) for i in ids]

    def entregas(self, id_mensaje):
        return self.pendientes.get(id_mensaje, 0)

    def confirmar(self, id_mensaje):
        self.pendientes.pop(id_mensaje, None)

    def mover_a_fallidos(self, id_mensaje, campos, error):
        self.fallidos.append({**campos, "error": error})
        self.confirmar(id_mensaje)

    def estado(self):
        return {"en_stream": len(self.mensajes), "sin_confirmar": len(self.pendientes),
                "fallidos": len(self.fallidos)}


class GrafoEnMemoria:
    """Registra los eventos aplicados; puede fallar N veces para probar reintentos."""

    def __init__(self, fallar_veces: int = 0, fallar_siempre_con: str | None = None):
        self.aplicados: list[tuple[str, dict]] = []
        self.fallar_veces = fallar_veces
        self.fallar_siempre_con = fallar_siempre_con

    def verificar(self):
        pass

    def aplicar_evento(self, tipo, payload, id_evento=None):
        if self.fallar_veces > 0:
            self.fallar_veces -= 1
            raise ConnectionError("Neo4j caído")
        if tipo == self.fallar_siempre_con:
            raise ValueError("evento con datos inválidos")
        self.aplicados.append((tipo, payload))

    def resumen(self):
        return [{"etiqueta": "Envio", "cantidad": 1}]


# =====================================================================
# Fase 3 — telemetría IoT
# =====================================================================
from app.dominio.modelos import Sensor  # noqa: E402


class SensoresEnMemoria:
    """Lo que la telemetría necesita de RepositorioContenedores."""

    def __init__(self, sensores: list[Sensor], envios_por_contenedor: dict[int, int] | None = None):
        self.sensores = {s.id_sensor: s for s in sensores}
        self.envios_por_contenedor = envios_por_contenedor or {}

    def sensor(self, id_sensor):
        return self.sensores.get(id_sensor)

    def sensores_instalados_activos(self):
        return [s for s in self.sensores.values() if s.estado == "ACTIVO" and s.id_contenedor is not None]

    def envio_en_curso_de_contenedor(self, id_contenedor):
        return self.envios_por_contenedor.get(id_contenedor)


class CassandraEnMemoria:
    def __init__(self, caida: bool = False):
        self.lecturas: list = []
        self.anomalias: list = []
        self.caida = caida

    def registrar_lectura(self, l):
        if self.caida:
            from app.excepciones import ErrorConexion
            raise ErrorConexion("Cassandra")
        self.lecturas.append(l)

    def registrar_anomalia(self, l, umbral_min, umbral_max):
        self.anomalias.append((l, umbral_min, umbral_max))

    def ultimas_lecturas(self, ids):
        ultimas = {}
        for l in sorted(self.lecturas, key=lambda x: x.fecha_hora):
            ultimas[l.id_sensor] = {"id_sensor": l.id_sensor, "fecha_hora": l.fecha_hora,
                                    "temperatura": l.temperatura, "bateria": l.bateria}
        return [v for k, v in ultimas.items() if k in ids]


class TelemetriaRedisEnMemoria:
    def __init__(self, caido: bool = False):
        self.contacto: dict[int, float] = {}
        self.silencios: set = set()
        self.stream: list[dict] = []
        self.caido = caido

    def _revisar(self):
        if self.caido:
            raise ConnectionError("Redis caído")

    def registrar_contacto(self, id_sensor, momento):
        self._revisar()
        self.contacto[id_sensor] = max(self.contacto.get(id_sensor, 0), momento.timestamp())

    def contactos(self):
        return dict(self.contacto)

    def reservar_alerta(self, tipo, id_sensor):
        self._revisar()
        if (tipo, id_sensor) in self.silencios:
            return False
        self.silencios.add((tipo, id_sensor))
        return True

    def liberar_alerta(self, tipo, id_sensor):
        self.silencios.discard((tipo, id_sensor))

    def publicar_alerta(self, campos):
        self._revisar()
        self.stream.append(campos)

    def asegurar_grupo(self):
        pass

    def alertas_nuevas(self, consumidor, cantidad=20):
        nuevas, self.stream = self.stream, []
        return nuevas


class AlertasEnMemoria:
    def __init__(self, caida: bool = False):
        self.alertas: list = []
        self.caida = caida

    def insertar(self, a):
        if self.caida:
            from app.excepciones import ErrorConexion
            raise ErrorConexion("MongoDB")
        a.id_alerta = f"{len(self.alertas) + 1:024x}"
        self.alertas.append(a)
        return a.id_alerta

    def listar(self, estados, limite=50):
        return [a for a in self.alertas if a.estado in estados]

    def cambiar_estado(self, id_alerta, estado):
        for a in self.alertas:
            if a.id_alerta == id_alerta:
                a.estado = estado
                return True
        return False


# =====================================================================
# Fase 4 — trazabilidad e incidentes
# =====================================================================
class EventosEnMemoria:
    def __init__(self, caido: bool = False):
        self.eventos: list = []
        self.origenes: set = set()
        self.caido = caido

    def verificar(self):
        if self.caido:
            from app.excepciones import ErrorConexion
            raise ErrorConexion("MongoDB")

    def registrar(self, e, id_evento_origen=None):
        self.verificar()
        if id_evento_origen is not None:
            if id_evento_origen in self.origenes:      # como el índice único + $setOnInsert
                return
            self.origenes.add(id_evento_origen)
        e.id_evento = f"{len(self.eventos) + 1:024x}"
        self.eventos.append(e)

    def de_envio(self, id_envio):
        self.verificar()
        return sorted([e for e in self.eventos if e.id_envio == id_envio], key=lambda e: e.fecha_hora)


class IncidentesEnMemoria:
    def __init__(self):
        self.docs: dict[str, object] = {}
        self.pendientes: set = set()
        self.agregaciones = 0

    def insertar(self, i, id_usuario):
        i.id_incidente = f"{len(self.docs) + 1:024x}"
        i.historial = [{"estado": i.estado, "fecha": i.fecha, "id_usuario": id_usuario}]
        self.docs[i.id_incidente] = i
        self.pendientes.add(i.id_incidente)
        return i.id_incidente

    def obtener(self, id_incidente):
        return self.docs.get(id_incidente)

    def cambiar_estado(self, id_incidente, estado, fecha, id_usuario):
        i = self.docs[id_incidente]
        i.estado = estado
        i.historial.append({"estado": estado, "fecha": fecha, "id_usuario": id_usuario})
        return True

    def listar(self, estados, limite=50):
        return [i for i in self.docs.values() if i.estado in estados]

    def de_envio(self, id_envio):
        return [i for i in self.docs.values() if i.id_envio == id_envio]

    def ranking_rutas(self, limite=10):
        self.agregaciones += 1
        rutas: dict = {}
        for i in self.docs.values():
            rutas.setdefault(i.ruta, 0)
            rutas[i.ruta] += 1
        return [{"ruta": r, "incidentes": n} for r, n in rutas.items()]

    def pendientes_grafo(self, limite=200):
        return [{"id_incidente": k, "id_envio": self.docs[k].id_envio, "id_tramo": self.docs[k].id_tramo,
                 "tipo": self.docs[k].tipo, "severidad": self.docs[k].severidad,
                 "fecha": self.docs[k].fecha.isoformat()} for k in sorted(self.pendientes)]

    def marcar_publicados(self, ids):
        self.pendientes -= set(ids)

    def todos_para_grafo(self):
        return []


class CacheEnMemoria:
    def __init__(self):
        self.datos: dict = {}

    def obtener(self, clave):
        return self.datos.get(clave)

    def guardar(self, clave, valor, segundos):
        self.datos[clave] = valor

    def borrar(self, clave):
        self.datos.pop(clave, None)


# =====================================================================
# Fase 5 — mensajería
# =====================================================================
from app.dominio.modelos import Conversacion as _Conv  # noqa: E402


class UsuariosListaEnMemoria:
    """Lo que la mensajería usa de RepositorioUsuarios: listar y obtener."""

    def __init__(self, usuarios):
        self.usuarios = {u.id_usuario: u for u in usuarios}

    def listar(self):
        return list(self.usuarios.values())

    def obtener(self, id_usuario):
        return self.usuarios.get(id_usuario)


class GruposEnMemoria:
    def __init__(self):
        self.grupos: dict = {}

    def crear(self, g):
        from app.excepciones import ErrorConflicto
        if any(x.nombre == g.nombre for x in self.grupos.values()):
            raise ErrorConflicto(f"Ya existe un grupo llamado {g.nombre}")
        g.id_grupo = f"g{len(self.grupos) + 1:023x}"
        self.grupos[g.id_grupo] = g
        return g.id_grupo

    def obtener(self, id_grupo):
        return copy.deepcopy(self.grupos.get(id_grupo))

    def de_usuario(self, id_usuario):
        return [copy.deepcopy(g) for g in self.grupos.values() if g.es_integrante(id_usuario)]

    def agregar_integrante(self, id_grupo, i):
        g = self.grupos[id_grupo]
        if g.es_integrante(i.id_usuario):
            return False
        g.integrantes.append(i)
        return True

    def quitar_integrante(self, id_grupo, id_usuario):
        g = self.grupos[id_grupo]
        g.integrantes = [i for i in g.integrantes if i.id_usuario != id_usuario]
        return True


class ConversacionesEnMemoria:
    def __init__(self):
        self.convs: dict = {}

    def _nueva(self, c):
        c.id_conversacion = f"c{len(self.convs) + 1:023x}"
        self.convs[c.id_conversacion] = c
        return c

    def privada(self, a, b, ahora):
        for c in self.convs.values():
            if c.tipo == "PRIVADA" and sorted(c.participantes) == sorted([a, b]):
                return copy.deepcopy(c)
        return copy.deepcopy(self._nueva(_Conv("PRIVADA", sorted([a, b]), ahora)))

    def crear_grupal(self, id_grupo, participantes, ahora):
        return self._nueva(_Conv("GRUPAL", list(participantes), ahora, id_grupo=id_grupo)).id_conversacion

    def de_grupo(self, id_grupo):
        return next((copy.deepcopy(c) for c in self.convs.values() if c.id_grupo == id_grupo), None)

    def obtener(self, id_conversacion):
        return copy.deepcopy(self.convs.get(id_conversacion))

    def bandeja(self, id_usuario, limite=30):
        lista = [c for c in self.convs.values() if id_usuario in c.participantes]
        return [copy.deepcopy(c) for c in sorted(lista, key=lambda c: c.ultimo_fecha or c.creada, reverse=True)]

    def actualizar_ultimo(self, id_conversacion, resumen, fecha, id_usuario):
        c = self.convs[id_conversacion]
        c.ultimo_resumen, c.ultimo_fecha, c.ultimo_id_usuario = resumen[:120], fecha, id_usuario

    def agregar_participante(self, id_conversacion, id_usuario):
        if id_usuario not in self.convs[id_conversacion].participantes:
            self.convs[id_conversacion].participantes.append(id_usuario)

    def quitar_participante(self, id_conversacion, id_usuario):
        self.convs[id_conversacion].participantes.remove(id_usuario)


class MensajesEnMemoria:
    def __init__(self):
        self.mensajes: list = []
        self.destinatarios: list = []

    def insertar(self, m, destinatario):
        m.id_mensaje = f"m{len(self.mensajes) + 1:023x}"
        self.mensajes.append(m)
        self.destinatarios.append(destinatario)
        return m.id_mensaje

    def ultimos(self, id_conversacion, limite=20):
        return [m for m in self.mensajes if m.id_conversacion == id_conversacion][-limite:]

    def marcar_leidos(self, id_conversacion, id_usuario):
        n = 0
        for m in self.mensajes:
            if m.id_conversacion == id_conversacion and id_usuario not in m.leido_por:
                m.leido_por.append(id_usuario)
                n += 1
        return n

    def no_leidos_por_conversacion(self, ids, id_usuario):
        conteo: dict = {}
        for m in self.mensajes:
            if m.id_conversacion in ids and id_usuario not in m.leido_por:
                conteo[m.id_conversacion] = conteo.get(m.id_conversacion, 0) + 1
        return conteo


class MensajeriaRedisEnMemoria:
    def __init__(self, limite: int = 20):
        self.hashes: dict = {}
        self.envios: dict = {}
        self.limite = limite
        self.conectados: set = set()
        self.caido = False

    def _revisar(self):
        if self.caido:
            raise ConnectionError("Redis caído")

    def sumar_no_leidos(self, ids, conv):
        self._revisar()
        for u in ids:
            h = self.hashes.setdefault(u, {})
            h[conv] = h.get(conv, 0) + 1

    def no_leidos(self, u):
        self._revisar()
        if u not in self.hashes:
            return None
        return {k: v for k, v in self.hashes[u].items() if v > 0}

    def reemplazar_no_leidos(self, u, conteos):
        self._revisar()
        self.hashes[u] = dict(conteos)

    def limpiar(self, u, conv):
        self._revisar()
        self.hashes.get(u, {}).pop(conv, None)

    def permitir_envio(self, u):
        self._revisar()
        self.envios[u] = self.envios.get(u, 0) + 1
        return self.envios[u] <= self.limite

    def en_linea(self, ids):
        self._revisar()
        return self.conectados & set(ids)
