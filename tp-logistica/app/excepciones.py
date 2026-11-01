"""Jerarquía de errores de la aplicación.

Los servicios lanzan estas excepciones con un mensaje pensado para el usuario.
La consola las atrapa y muestra el mensaje; cualquier otra excepción (un error
inesperado) se registra completa en el log y al usuario se le muestra un aviso
genérico, nunca un traceback.
"""


class ErrorAplicacion(Exception):
    """Base de todos los errores conocidos de la aplicación."""


class ErrorValidacion(ErrorAplicacion):
    """Un dato ingresado no cumple una regla (formato, rango, obligatorio)."""


class EntidadNoEncontrada(ErrorAplicacion):
    """Se buscó algo por su identificador y no existe."""


class ErrorConflicto(ErrorAplicacion):
    """La operación choca con datos existentes (por ejemplo, un CUIT repetido)."""


class ErrorAutenticacion(ErrorAplicacion):
    """Usuario o contraseña incorrectos, usuario bloqueado o sesión vencida."""


class ErrorPermiso(ErrorAplicacion):
    """El usuario no tiene el nivel de permisos necesario para la operación."""


class ErrorConexion(ErrorAplicacion):
    """Un motor de base de datos no responde."""

    def __init__(self, motor: str, detalle: str = ""):
        self.motor = motor
        super().__init__(f"No se pudo conectar con {motor}. Verificá que el contenedor esté levantado"
                         + (f" ({detalle})" if detalle else "."))
