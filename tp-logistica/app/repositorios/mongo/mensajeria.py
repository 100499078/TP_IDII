"""Mensajería en MongoDB: grupos_trabajo, conversaciones y mensajes.

- grupos_trabajo: los integrantes van EMBEBIDOS (lista acotada, máximo 200 por validador)
  y siempre se leen junto con el grupo.
- conversaciones: la "cabecera" de cada chat, con una copia del último mensaje para que la
  bandeja de entrada se arme con una sola consulta (índice ix_bandeja).
- mensajes: colección APARTE porque crece sin límite (embeberlos en la conversación haría
  documentos gigantes; MongoDB tiene un tope de 16 MB por documento).

MongoDB en Docker corre como servidor único (sin réplicas), así que no usamos transacciones
de varios documentos: enviar un mensaje son dos escrituras (mensaje + cabecera) y la
cabecera se puede recalcular si la segunda fallara.
"""
from __future__ import annotations

from datetime import datetime

from bson import ObjectId

from app.dominio.modelos import Conversacion, GrupoTrabajo, Integrante, Mensaje
from app.excepciones import ErrorConexion, ErrorConflicto
from app.infra.conexiones import GestorConexiones


def _ejecutar(funcion):
    from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError
    try:
        return funcion()
    except (ServerSelectionTimeoutError, ConnectionFailure) as exc:
        raise ErrorConexion("MongoDB") from exc


def _a_grupo(d: dict) -> GrupoTrabajo:
    return GrupoTrabajo(nombre=d["nombre"], descripcion=d.get("descripcion", ""), creado_por=d["creado_por"],
                        fecha_creacion=d["fecha_creacion"], activo=d["activo"], id_grupo=str(d["_id"]),
                        integrantes=[Integrante(i["id_usuario"], i.get("nombre", ""), i["rol_en_grupo"], i["desde"])
                                     for i in d["integrantes"]])


def _a_conversacion(d: dict) -> Conversacion:
    u = d.get("ultimo_mensaje") or {}
    return Conversacion(tipo=d["tipo"], participantes=d["participantes"], creada=d["creada"],
                        id_grupo=str(d["id_grupo"]) if d.get("id_grupo") else None, ultimo_resumen=u.get("resumen"),
                        ultimo_fecha=u.get("fecha"), ultimo_id_usuario=u.get("id_usuario"),
                        id_conversacion=str(d["_id"]))


def _a_mensaje(d: dict) -> Mensaje:
    return Mensaje(id_conversacion=str(d["id_conversacion"]), id_remitente=d["remitente"]["id_usuario"],
                   nombre_remitente=d["remitente"].get("nombre", ""), fecha_hora=d["fecha_hora"],
                   contenido=d["contenido"], tipo=d["tipo"], leido_por=d.get("leido_por", []),
                   id_mensaje=str(d["_id"]))


class RepositorioGrupos:
    def __init__(self, conexiones: GestorConexiones):
        self._cx = conexiones

    @property
    def _col(self):
        return self._cx.mongo["grupos_trabajo"]

    def crear(self, g: GrupoTrabajo) -> str:
        from pymongo.errors import DuplicateKeyError
        doc = {"nombre": g.nombre, "descripcion": g.descripcion, "creado_por": g.creado_por,
               "fecha_creacion": g.fecha_creacion, "activo": g.activo,
               "integrantes": [{"id_usuario": i.id_usuario, "nombre": i.nombre, "rol_en_grupo": i.rol_en_grupo,
                                "desde": i.desde} for i in g.integrantes]}
        try:
            g.id_grupo = str(_ejecutar(lambda: self._col.insert_one(doc)).inserted_id)
        except DuplicateKeyError as exc:                     # índice único ux_nombre
            raise ErrorConflicto(f"Ya existe un grupo llamado {g.nombre}") from exc
        return g.id_grupo

    def obtener(self, id_grupo: str) -> GrupoTrabajo | None:
        d = _ejecutar(lambda: self._col.find_one({"_id": ObjectId(id_grupo)}))
        return _a_grupo(d) if d else None

    def de_usuario(self, id_usuario: int) -> list[GrupoTrabajo]:
        cursor = self._col.find({"integrantes.id_usuario": id_usuario, "activo": True}).sort("nombre", 1)
        return [_a_grupo(d) for d in _ejecutar(lambda: list(cursor))]                # ix_integrante

    def agregar_integrante(self, id_grupo: str, i: Integrante) -> bool:
        # El filtro "$ne" evita agregar dos veces al mismo usuario (la condición y el cambio son atómicos)
        r = _ejecutar(lambda: self._col.update_one(
            {"_id": ObjectId(id_grupo), "integrantes.id_usuario": {"$ne": i.id_usuario}},
            {"$push": {"integrantes": {"id_usuario": i.id_usuario, "nombre": i.nombre,
                                       "rol_en_grupo": i.rol_en_grupo, "desde": i.desde}}}))
        return r.modified_count == 1

    def quitar_integrante(self, id_grupo: str, id_usuario: int) -> bool:
        r = _ejecutar(lambda: self._col.update_one({"_id": ObjectId(id_grupo)},
                                                   {"$pull": {"integrantes": {"id_usuario": id_usuario}}}))
        return r.modified_count == 1


class RepositorioConversaciones:
    def __init__(self, conexiones: GestorConexiones):
        self._cx = conexiones
        self._indices_listos = False

    @property
    def _col(self):
        col = self._cx.mongo["conversaciones"]
        if not self._indices_listos:
            # Una sola conversación privada por par de usuarios: clave "menor-mayor" con índice único
            _ejecutar(lambda: col.create_index("clave_privada", name="ux_clave_privada", unique=True,
                                               partialFilterExpression={"clave_privada": {"$exists": True}}))
            self._indices_listos = True
        return col

    def privada(self, a: int, b: int, ahora: datetime) -> Conversacion:
        """Devuelve la conversación privada entre a y b; si no existe, la crea (upsert idempotente)."""
        clave = f"{min(a, b)}-{max(a, b)}"
        doc = _ejecutar(lambda: self._col.find_one_and_update(
            {"clave_privada": clave},
            {"$setOnInsert": {"tipo": "PRIVADA", "participantes": sorted([a, b]), "creada": ahora,
                              "clave_privada": clave}},
            upsert=True, return_document=True))
        return _a_conversacion(doc)

    def crear_grupal(self, id_grupo: str, participantes: list[int], ahora: datetime) -> str:
        r = _ejecutar(lambda: self._col.insert_one({"tipo": "GRUPAL", "participantes": participantes,
                                                    "id_grupo": ObjectId(id_grupo), "creada": ahora}))
        return str(r.inserted_id)

    def de_grupo(self, id_grupo: str) -> Conversacion | None:
        d = _ejecutar(lambda: self._col.find_one({"id_grupo": ObjectId(id_grupo)}))       # ix_grupo
        return _a_conversacion(d) if d else None

    def obtener(self, id_conversacion: str) -> Conversacion | None:
        d = _ejecutar(lambda: self._col.find_one({"_id": ObjectId(id_conversacion)}))
        return _a_conversacion(d) if d else None

    def bandeja(self, id_usuario: int, limite: int = 30) -> list[Conversacion]:
        """Las conversaciones del usuario, la de actividad más reciente primero (índice ix_bandeja)."""
        cursor = (self._col.find({"participantes": id_usuario})
                  .sort([("ultimo_mensaje.fecha", -1), ("creada", -1)]).limit(limite))
        return [_a_conversacion(d) for d in _ejecutar(lambda: list(cursor))]

    def actualizar_ultimo(self, id_conversacion: str, resumen: str, fecha: datetime, id_usuario: int) -> None:
        _ejecutar(lambda: self._col.update_one(
            {"_id": ObjectId(id_conversacion)},
            {"$set": {"ultimo_mensaje": {"resumen": resumen[:120], "fecha": fecha, "id_usuario": id_usuario}}}))

    def agregar_participante(self, id_conversacion: str, id_usuario: int) -> None:
        _ejecutar(lambda: self._col.update_one({"_id": ObjectId(id_conversacion)},
                                               {"$addToSet": {"participantes": id_usuario}}))

    def quitar_participante(self, id_conversacion: str, id_usuario: int) -> None:
        _ejecutar(lambda: self._col.update_one({"_id": ObjectId(id_conversacion)},
                                               {"$pull": {"participantes": id_usuario}}))


class RepositorioMensajes:
    def __init__(self, conexiones: GestorConexiones):
        self._cx = conexiones

    @property
    def _col(self):
        return self._cx.mongo["mensajes"]

    def insertar(self, m: Mensaje, destinatario: dict) -> str:
        if "id_grupo" in destinatario:                   # en MongoDB el id del grupo es un ObjectId
            destinatario = {**destinatario, "id_grupo": ObjectId(destinatario["id_grupo"])}
        doc = {"id_conversacion": ObjectId(m.id_conversacion),
               "remitente": {"id_usuario": m.id_remitente, "nombre": m.nombre_remitente},
               "destinatario": destinatario, "fecha_hora": m.fecha_hora, "contenido": m.contenido,
               "tipo": m.tipo, "leido_por": m.leido_por}
        m.id_mensaje = str(_ejecutar(lambda: self._col.insert_one(doc)).inserted_id)
        return m.id_mensaje

    def ultimos(self, id_conversacion: str, limite: int = 20) -> list[Mensaje]:
        """Los últimos `limite` mensajes en orden cronológico (índice ix_conversacion_fecha)."""
        cursor = self._col.find({"id_conversacion": ObjectId(id_conversacion)}).sort("fecha_hora", -1).limit(limite)
        return list(reversed([_a_mensaje(d) for d in _ejecutar(lambda: list(cursor))]))

    def marcar_leidos(self, id_conversacion: str, id_usuario: int) -> int:
        r = _ejecutar(lambda: self._col.update_many(
            {"id_conversacion": ObjectId(id_conversacion), "leido_por": {"$ne": id_usuario}},
            {"$addToSet": {"leido_por": id_usuario}}))
        return r.modified_count

    def no_leidos_por_conversacion(self, ids_conversacion: list[str], id_usuario: int) -> dict[str, int]:
        """Cuenta en MongoDB (la fuente de verdad) los mensajes que el usuario no leyó."""
        if not ids_conversacion:
            return {}
        pipeline = [{"$match": {"id_conversacion": {"$in": [ObjectId(i) for i in ids_conversacion]},
                                "leido_por": {"$ne": id_usuario}}},
                    {"$group": {"_id": "$id_conversacion", "n": {"$sum": 1}}}]
        return {str(f["_id"]): f["n"] for f in _ejecutar(lambda: list(self._col.aggregate(pipeline)))}
