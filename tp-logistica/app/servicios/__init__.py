"""Capa de servicios: reglas de negocio y coordinación entre motores.

Un servicio no escribe SQL ni comandos Redis: llama a repositorios. Cuando una
operación toca más de un motor, el servicio decide el orden y qué hacer si
una parte falla.
"""
