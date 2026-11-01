"""Base de los menús: cada opción declara su función y el nivel de permisos que exige.

    OPCIONES = [("1", "Listar clientes", "listar", 2), ...]

`mostrar()` muestra solo las opciones que el usuario puede usar según su nivel, y
ejecuta la elegida a través de `ejecutar` (la función de la consola que valida
la sesión, exige el nivel y maneja los errores).
"""
from __future__ import annotations

from typing import Callable

from app.dominio.modelos import Sesion
from app.presentacion import componentes as ui


class MenuBase:
    TITULO = ""
    OPCIONES: list[tuple[str, str, str, int]] = []   # (clave, texto, nombre del método, nivel mínimo)

    def __init__(self, ejecutar: Callable, sesion_actual: Callable[[], Sesion]):
        self._ejecutar = ejecutar
        self._sesion_actual = sesion_actual

    def mostrar(self, sesion: Sesion) -> None:
        while True:
            nivel = self._sesion_actual().nivel_permisos
            visibles = [(c, t, m, n) for c, t, m, n in self.OPCIONES if nivel >= n]
            opcion = ui.menu(self.TITULO, [(c, t) for c, t, _, _ in visibles] + [("0", "Volver")])
            if opcion == "0":
                return
            _, _, metodo, nivel_minimo = next(x for x in visibles if x[0] == opcion)
            self._ejecutar(getattr(self, metodo), nivel_minimo)
