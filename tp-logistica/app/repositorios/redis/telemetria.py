"""Redis en la telemetría: último contacto de cada sensor y cola de alertas.

- sensor:ultimo_contacto  -> SORTED SET: miembro = id_sensor, puntaje = epoch del último contacto.
                             "Sensores sin contacto desde hace X minutos" = ZRANGEBYSCORE -inf (limite.
- alertas:stream          -> STREAM con el grupo "notificadores": cada alerta nueva se entrega a
                             UNA consola de operador (cola de trabajo) y se confirma con XACK.
- alerta:{tipo}:{sensor}  -> STRING con TTL (SET NX EX): evita mandar la misma alerta en cada lectura.
"""
from __future__ import annotations

from datetime import datetime

from app.infra.conexiones import GestorConexiones

CONTACTO = "sensor:ultimo_contacto"
ALERTAS = "alertas:stream"
GRUPO_ALERTAS = "notificadores"
SILENCIO_SEGUNDOS = 900          # una misma alerta (tipo + sensor) como mucho cada 15 minutos


class RepositorioTelemetriaRedis:
    def __init__(self, conexiones: GestorConexiones):
        self._cx = conexiones
        self._grupo_listo = False

    # ------------------------------------------------------------------ último contacto
    def registrar_contacto(self, id_sensor: int, momento: datetime) -> None:
        # gt=True: solo actualiza si el nuevo puntaje es MAYOR (una lectura vieja no "atrasa" el contacto)
        self._cx.redis.zadd(CONTACTO, {str(id_sensor): momento.timestamp()}, gt=True)

    def contactos(self) -> dict[int, float]:
        """{id_sensor: epoch del último contacto} de todos los sensores que alguna vez reportaron."""
        return {int(m): s for m, s in self._cx.redis.zrange(CONTACTO, 0, -1, withscores=True)}

    def sin_contacto_desde(self, limite: datetime) -> dict[int, float]:
        """Sensores cuyo último contacto es ANTERIOR a `limite` (el "(" excluye el límite)."""
        filas = self._cx.redis.zrangebyscore(CONTACTO, "-inf", f"({limite.timestamp()}", withscores=True)
        return {int(m): s for m, s in filas}

    # ------------------------------------------------------------------ alertas
    def reservar_alerta(self, tipo: str, id_sensor: int) -> bool:
        """True si se puede emitir la alerta; False si ya se emitió una igual hace menos de 15 min.
        SET ... NX EX es atómico: dos procesos a la vez no pueden obtener True los dos."""
        return bool(self._cx.redis.set(f"alerta:{tipo}:{id_sensor}", "1", nx=True, ex=SILENCIO_SEGUNDOS))

    def liberar_alerta(self, tipo: str, id_sensor: int) -> None:
        self._cx.redis.delete(f"alerta:{tipo}:{id_sensor}")

    def publicar_alerta(self, campos: dict) -> str:
        # El grupo tiene que existir ANTES del primer aviso: un grupo nuevo empieza a leer
        # desde "$" (lo que llegue después de crearlo) y no vería los avisos anteriores.
        self.asegurar_grupo()
        datos = {k: "" if v is None else str(v) for k, v in campos.items()}
        return self._cx.redis.xadd(ALERTAS, datos, maxlen=10_000, approximate=True)

    def asegurar_grupo(self) -> None:
        if self._grupo_listo:
            return
        import redis
        try:
            self._cx.redis.xgroup_create(ALERTAS, GRUPO_ALERTAS, id="$", mkstream=True)
        except redis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):        # BUSYGROUP = ya existía
                raise
        self._grupo_listo = True

    def alertas_nuevas(self, consumidor: str, cantidad: int = 20) -> list[dict]:
        """Lee alertas que todavía no recibió ningún consumidor del grupo y las confirma."""
        respuesta = self._cx.redis.xreadgroup(GRUPO_ALERTAS, consumidor, {ALERTAS: ">"}, count=cantidad)
        mensajes = respuesta[0][1] if respuesta else []
        if mensajes:
            self._cx.redis.xack(ALERTAS, GRUPO_ALERTAS, *[i for i, _ in mensajes])
        return [campos for _, campos in mensajes]
