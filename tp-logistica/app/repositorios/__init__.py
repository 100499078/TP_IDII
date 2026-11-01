"""Capa de acceso a datos (patrón Repository): un subpaquete por motor.

Cada repositorio expone operaciones del negocio (buscar_por_email, guardar...)
y esconde el lenguaje de su motor (SQL, MQL, Cypher, CQL, comandos Redis).
"""
