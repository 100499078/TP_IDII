"""Validaciones de formato: CUIT, contenedor ISO 6346, UN/LOCODE, email."""
import pytest

from app.dominio.validaciones import (calcular_digito_cuit, validar_codigo_contenedor, validar_cuit,
                                      validar_email, validar_locode)


@pytest.mark.parametrize("cuit", ["30-71111111-1", "30-71222222-7", "20-12345678-6"])
def test_cuits_validos(cuit):
    assert validar_cuit(cuit) is None


def test_cuit_con_digito_incorrecto():
    assert "verificador" in validar_cuit("30-71111111-2")


def test_cuit_con_formato_incorrecto():
    assert "formato" in validar_cuit("30711111111")


def test_calcular_digito_cuit():
    assert calcular_digito_cuit("3071111111") == 1


@pytest.mark.parametrize("codigo", ["CSQU3054383", "MSCU1234566", "TGHU3456780"])
def test_contenedores_validos(codigo):
    assert validar_codigo_contenedor(codigo) is None


def test_contenedor_con_control_incorrecto_dice_el_correcto():
    assert "debería ser 3" in validar_codigo_contenedor("CSQU3054384")


def test_contenedor_con_categoria_invalida():
    assert validar_codigo_contenedor("CSQX3054383") is not None   # la 4.ª letra debe ser U, J o Z


def test_locode_y_email():
    assert validar_locode("ARBUE") is None
    assert validar_locode("AR-BUE") is not None
    assert validar_email("a@b.com") is None
    assert validar_email("sin-arroba") is not None
