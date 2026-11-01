"""Hash de contraseñas con bcrypt.

Nunca se guarda la contraseña: se guarda un hash con "sal" aleatoria. Para
verificar, bcrypt vuelve a calcular el hash con la sal guardada y compara.
"""
import bcrypt


def hashear(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii")


def verificar(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))
    except ValueError:          # hash con formato inválido en la base
        return False
