"""Validaciones de formato de los códigos del negocio.

Cada función devuelve None si el valor es válido, o un mensaje de error.
Así se pueden usar tanto en los servicios como en la consola (al pedir un dato).
"""
from __future__ import annotations

import re

_RE_CUIT = re.compile(r"^\d{2}-\d{8}-\d$")
_RE_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_RE_PAIS = re.compile(r"^[A-Z]{2}$")
_RE_LOCODE = re.compile(r"^[A-Z]{2}[A-Z2-9]{3}$")
_RE_CONTENEDOR = re.compile(r"^[A-Z]{3}[UJZ]\d{7}$")


def validar_cuit(cuit: str) -> str | None:
    """CUIT argentino con formato 30-12345678-9 y dígito verificador (módulo 11)."""
    if not _RE_CUIT.match(cuit or ""):
        return "El CUIT debe tener el formato 30-12345678-9"
    digitos = [int(c) for c in cuit.replace("-", "")]
    pesos = [5, 4, 3, 2, 7, 6, 5, 4, 3, 2]
    resto = sum(d * p for d, p in zip(digitos[:10], pesos)) % 11
    verificador = 0 if resto == 0 else 11 - resto
    if verificador == 10 or verificador != digitos[10]:
        return "El dígito verificador del CUIT no es correcto"
    return None


def calcular_digito_cuit(primeros_diez: str) -> int | None:
    """Devuelve el dígito verificador para 10 dígitos (None si no existe uno válido)."""
    pesos = [5, 4, 3, 2, 7, 6, 5, 4, 3, 2]
    resto = sum(int(d) * p for d, p in zip(primeros_diez, pesos)) % 11
    v = 0 if resto == 0 else 11 - resto
    return None if v == 10 else v


def validar_email(email: str) -> str | None:
    return None if _RE_EMAIL.match(email or "") else "El email no tiene un formato válido"


def validar_pais(pais: str) -> str | None:
    return None if _RE_PAIS.match(pais or "") else "El país debe ser un código ISO de 2 letras en mayúsculas (AR, ES...)"


def validar_locode(codigo: str) -> str | None:
    return None if _RE_LOCODE.match(codigo or "") else "El código UN/LOCODE tiene 5 caracteres: país + 3 (ej. ARBUE)"


def validar_codigo_contenedor(codigo: str) -> str | None:
    """Código ISO 6346: 3 letras del dueño + categoría (U, J o Z) + 6 dígitos + dígito de control.

    El dígito de control se calcula así: cada letra vale 10, 12, 13... (se saltean
    los múltiplos de 11) y cada dígito su valor; se multiplica cada posición por
    2^posición, se suma, y el resto de dividir por 11 (y luego por 10) es el control.
    """
    if not _RE_CONTENEDOR.match(codigo or ""):
        return "El código ISO 6346 tiene 3 letras + U/J/Z + 7 dígitos (ej. CSQU3054383)"
    valores = {}
    v = 10
    for letra in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        if v % 11 == 0:
            v += 1
        valores[letra] = v
        v += 1
    suma = sum((valores[c] if c.isalpha() else int(c)) * (2 ** i) for i, c in enumerate(codigo[:10]))
    control = suma % 11 % 10
    if control != int(codigo[10]):
        return f"El dígito de control del contenedor no es correcto (debería ser {control})"
    return None
