"""Database seeds — popula o banco com dados iniciais.

**A senha não fica mais no código.** Havia aqui ``password_hash=hash_password("123456")``
com SHA-256 e salt fixo — ou seja, a senha do dono estava no repositório, em um
formato que uma GPU quebra em segundos, e nada nunca a trocou porque não existia
tela de login. Agora o hash é PBKDF2 (``api/auth/passwords.py``) e a senha vem de
``NEO_SEED_PASSWORD``; sem a variável, o usuário é criado **sem senha** (não
consegue logar) e a seed diz como definir uma.

Definir/trocar senha depois: ``python -m scripts.set_password <usuário>``.
"""

import os

from sqlmodel import Session, create_engine, select

from api.auth.passwords import hash_password
from db.models.user import User

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://neo:neo@localhost:5434/neodb")


def seed_users(session: Session) -> None:
    senha = os.environ.get("NEO_SEED_PASSWORD") or ""
    users = [
        {
            "username": "daniel",
            "display_name": "Daniel Morais",
            "email": "daniel@neo.dev",
            "password_hash": hash_password(senha) if senha else None,
        },
    ]

    for data in users:
        existing = session.exec(
            select(User).where(User.username == data["username"])
        ).first()

        if existing:
            print(f"  [skip] user '{data['username']}' já existe")
            continue

        user = User(**data)
        session.add(user)
        if data["password_hash"]:
            print(f"  [created] user '{data['username']}' com senha de NEO_SEED_PASSWORD")
        else:
            print(
                f"  [created] user '{data['username']}' SEM senha — defina uma com "
                f"`python -m scripts.set_password {data['username']}`"
            )

    session.commit()


def run() -> None:
    print("Running seeds...")
    engine = create_engine(DATABASE_URL, echo=False)

    with Session(engine) as session:
        seed_users(session)

    print("Seeds concluídos.")


if __name__ == "__main__":
    run()
