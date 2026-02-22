"""Database seeds — popula o banco com dados iniciais."""

import hashlib
import os

from sqlmodel import Session, create_engine, select
from db.models.user import User


DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://neo:neo@localhost:5434/neodb")


def hash_password(password: str) -> str:
    """Hash simples com SHA-256 + salt."""
    salt = "neo-salt"
    return hashlib.sha256(f"{salt}{password}".encode()).hexdigest()


def seed_users(session: Session) -> None:
    users = [
        {
            "username": "daniel",
            "display_name": "Daniel Morais",
            "email": "daniel@neo.dev",
            "password_hash": hash_password("123456"),
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
        print(f"  [created] user '{data['username']}'")

    session.commit()


def run() -> None:
    print("Running seeds...")
    engine = create_engine(DATABASE_URL, echo=False)

    with Session(engine) as session:
        seed_users(session)

    print("Seeds concluídos.")


if __name__ == "__main__":
    run()
