"""Piezas reutilizables de la consola: títulos, tablas, pedidos de datos y mensajes."""
from __future__ import annotations

from typing import Any, Callable, Iterable, Sequence

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

consola = Console()


def titulo(texto: str, subtitulo: str | None = None) -> None:
    consola.print()
    consola.print(Panel.fit(f"[bold]{texto}[/bold]" + (f"\n[dim]{subtitulo}[/dim]" if subtitulo else ""),
                            border_style="blue"))


def tabla(columnas: Sequence[str], filas: Iterable[Sequence[Any]], titulo_tabla: str | None = None) -> None:
    t = Table(title=titulo_tabla, header_style="bold blue", show_lines=False)
    for c in columnas:
        t.add_column(c)
    cantidad = 0
    for fila in filas:
        t.add_row(*["" if v is None else str(v) for v in fila])
        cantidad += 1
    if cantidad == 0:
        info("No hay datos para mostrar.")
    else:
        consola.print(t)


def menu(titulo_menu: str, opciones: Sequence[tuple[str, str]]) -> str:
    """Muestra un menú numerado y devuelve la clave elegida.
    opciones: [(clave, texto), ...]; la clave '0' se usa para volver/salir."""
    consola.print(f"\n[bold]{titulo_menu}[/bold]")
    for clave, texto in opciones:
        consola.print(f"  [cyan]{clave:>2}[/cyan]  {texto}")
    validas = {c.lower(): c for c, _ in opciones}          # acepta "e" o "E"
    while True:
        elegida = (Prompt.ask("Opción") or "").strip().lower()
        if elegida in validas:
            return validas[elegida]
        error("Opción inválida.")


def pedir(texto: str, obligatorio: bool = True, defecto: str | None = None,
          validar: Callable[[str], str | None] | None = None, oculto: bool = False) -> str:
    """Pide un texto. `validar` devuelve un mensaje de error o None si está bien."""
    while True:
        valor = Prompt.ask(texto, default=defecto, password=oculto) or ""
        valor = valor.strip()
        if obligatorio and not valor:
            error("Este dato es obligatorio.")
            continue
        if valor and validar:
            problema = validar(valor)
            if problema:
                error(problema)
                continue
        return valor


def pedir_entero(texto: str, minimo: int | None = None, maximo: int | None = None,
                 obligatorio: bool = True) -> int | None:
    while True:
        valor = Prompt.ask(texto, default=None if obligatorio else "").strip()
        if not valor and not obligatorio:
            return None
        try:
            n = int(valor)
        except ValueError:
            error("Ingresá un número entero.")
            continue
        if minimo is not None and n < minimo:
            error(f"El valor debe ser mayor o igual a {minimo}.")
            continue
        if maximo is not None and n > maximo:
            error(f"El valor debe ser menor o igual a {maximo}.")
            continue
        return n


def pedir_opcion(texto: str, opciones: Sequence[str], defecto: str | None = None) -> str:
    """Elige un valor de una lista cerrada (se muestra entre corchetes)."""
    return Prompt.ask(f"{texto} [dim]({' / '.join(opciones)})[/dim]", choices=list(opciones),
                      default=defecto, show_choices=False)


def pedir_decimal(texto: str, minimo: float | None = None, obligatorio: bool = True,
                  defecto: str | None = None) -> float | None:
    while True:
        valor = (Prompt.ask(texto, default=defecto if defecto is not None else (None if obligatorio else "")) or "").strip()
        if not valor and not obligatorio:
            return None
        try:
            n = float(valor.replace(",", "."))
        except ValueError:
            error("Ingresá un número (podés usar coma o punto decimal).")
            continue
        if minimo is not None and n < minimo:
            error(f"El valor debe ser mayor o igual a {minimo}.")
            continue
        return n


def pedir_fecha_hora(texto: str, defecto: str | None = None):
    """Fecha y hora en UTC con formato AAAA-MM-DD HH:MM (también acepta solo AAAA-MM-DD)."""
    from datetime import datetime, timezone
    while True:
        valor = (Prompt.ask(f"{texto} [dim](AAAA-MM-DD HH:MM, UTC)[/dim]", default=defecto) or "").strip()
        for formato in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(valor, formato).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        error("Formato inválido. Ejemplo: 2026-11-15 14:30")


def pedir_lista_enteros(texto: str) -> list[int]:
    while True:
        valor = Prompt.ask(f"{texto} [dim](separados por coma)[/dim]").strip()
        try:
            return [int(x) for x in valor.replace(" ", "").split(",") if x]
        except ValueError:
            error("Ingresá números separados por coma, por ejemplo: 3,7")


def fecha(valor) -> str:
    """Formatea una fecha u hora para las tablas."""
    if valor is None:
        return "—"
    return valor.strftime("%Y-%m-%d %H:%M") if hasattr(valor, "hour") else valor.strftime("%Y-%m-%d")


def dinero(valor) -> str:
    return "—" if valor is None else f"$ {float(valor):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


class _Confirmar(Confirm):
    """Confirm de rich, pero en castellano: s/n en lugar de y/n."""
    choices = ["s", "n"]
    validate_error_message = "[prompt.invalid]Respondé s o n"

    def process_response(self, value: str) -> bool:
        value = value.strip().lower()
        if value in ("s", "si", "sí"):
            return True
        if value in ("n", "no"):
            return False
        from rich.prompt import InvalidResponse
        raise InvalidResponse(self.validate_error_message)

    def render_default(self, default):
        from rich.text import Text
        return Text(f"({'s' if default else 'n'})", "prompt.default")


def confirmar(texto: str, defecto: bool = False) -> bool:
    return _Confirmar.ask(texto, default=defecto)


def ok(texto: str) -> None:
    consola.print(f"[green]✔ {texto}[/green]")


def error(texto: str) -> None:
    consola.print(f"[red]✘ {texto}[/red]")


def info(texto: str) -> None:
    consola.print(f"[dim]{texto}[/dim]")


def pausa() -> None:
    Prompt.ask("[dim]Enter para continuar[/dim]", default="", show_default=False)
