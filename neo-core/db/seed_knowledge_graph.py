"""Seed knowledge graph — Lucas Mendes (usuário fictício para testes)."""

import os
from uuid import uuid4

from sqlmodel import Session, create_engine, select

from db.models.user import User
from db.models.knowledge_graph import KnowledgeEntity, KnowledgeRelation


DATABASE_URL = os.environ.get("DATABASE_URL_SYNC", os.environ.get("DATABASE_URL", "postgresql://neo:neo@localhost:5434/neodb"))
# Strip asyncpg driver if present (seed uses sync)
if "asyncpg" in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgresql+asyncpg", "postgresql")


def _entity(user_id, etype, name, props=None):
    return KnowledgeEntity(id=uuid4(), user_id=user_id, type=etype, name=name, properties=props)


def _relation(user_id, source, target, rtype, props=None):
    return KnowledgeRelation(id=uuid4(), user_id=user_id, source_id=source.id, target_id=target.id, type=rtype, properties=props)


def seed_knowledge_graph(session: Session) -> None:
    # ── Usuário "lucas" ──────────────────────────────────────────
    user = session.exec(select(User).where(User.username == "lucas")).first()
    if not user:
        from db.seeds import hash_password
        user = User(username="lucas", display_name="Lucas Mendes", email="lucas@neo.dev", password_hash=hash_password("123456"))
        session.add(user)
        session.flush()
        print("  [created] user 'lucas'")
    else:
        # Limpar dados anteriores para re-seed
        session.exec(  # type: ignore[call-overload]
            KnowledgeRelation.__table__.delete().where(KnowledgeRelation.user_id == user.id)
        )
        session.exec(  # type: ignore[call-overload]
            KnowledgeEntity.__table__.delete().where(KnowledgeEntity.user_id == user.id)
        )
        session.flush()
        print("  [reset] dados anteriores de 'lucas' removidos")

    uid = user.id

    # ── Nó de perfil (global) ────────────────────────────────────
    perfil = _entity(uid, "perfil", "Lucas Mendes", {"idade": 28, "peso_kg": 78.5, "altura_cm": 178, "sexo": "M"})

    # ── Nós globais ──────────────────────────────────────────────
    saude = _entity(uid, "saude", "Estado Geral", {"energia": 7, "sono_h": 7, "estresse": "moderado"})
    restricao = _entity(uid, "restricao", "Lactose", {"tipo": "intolerancia", "severidade": "moderada"})
    rotina = _entity(uid, "rotina", "Dia Útil", {"hora_acordar": "06:00", "hora_treino": "07:00", "hora_dormir": "23:00"})

    # ── Contextos ────────────────────────────────────────────────
    ctx_treino = _entity(uid, "contexto", "Treino de Academia", {"descricao": "Tudo relacionado ao treino na academia"})
    ctx_sabado = _entity(uid, "contexto", "Sábado à Noite", {"descricao": "Lazer, janta fora, social"})
    ctx_trabalho = _entity(uid, "contexto", "Trabalho Remoto", {"descricao": "Rotina de trabalho em casa"})

    # ── Treino de Academia — filhos ──────────────────────────────
    ref_pre = _entity(uid, "refeicao", "Pré-Treino", {"horario": "06:30", "calorias": 280, "tempo_preparo_min": 5})
    ref_pos = _entity(uid, "refeicao", "Pós-Treino", {"horario": "08:15", "calorias": 210})
    treino_peito = _entity(uid, "treino", "Peito e Tríceps", {"tipo": "musculacao", "intensidade": "alta", "duracao_min": 60})
    treino_costas = _entity(uid, "treino", "Costas e Bíceps", {"tipo": "musculacao", "intensidade": "alta", "duracao_min": 55})
    treino_corrida = _entity(uid, "treino", "Corrida Leve", {"tipo": "cardio", "intensidade": "moderada", "duracao_min": 35})
    supl_creatina = _entity(uid, "suplemento", "Creatina", {"dose_g": 5, "horario": "pos_treino", "em_uso": True})
    obj_hiper = _entity(uid, "objetivo", "Hipertrofia", {"prioridade": "alta", "meta_kg": 82, "prazo": "2026-06"})

    # ── Alimentos (pré-treino) ───────────────────────────────────
    banana = _entity(uid, "alimento", "Banana", {"categoria": "fruta", "calorias_100g": 89})
    aveia = _entity(uid, "alimento", "Aveia", {"categoria": "cereal", "calorias_100g": 68})
    cafe = _entity(uid, "alimento", "Café preto", {"categoria": "bebida", "calorias_100g": 2})

    # ── Alimentos (pós-treino) ───────────────────────────────────
    whey = _entity(uid, "alimento", "Whey Isolado", {"categoria": "suplemento", "sem_lactose": True})
    leite_amend = _entity(uid, "alimento", "Leite de Amêndoas", {"categoria": "bebida", "sem_lactose": True})

    # ── Sábado à Noite — filhos ──────────────────────────────────
    ref_janta = _entity(uid, "refeicao", "Janta com Amigos", {"tipo": "social", "horario": "20:00"})
    pref_rodizio = _entity(uid, "preferencia", "Gosta de rodízio", {"tipo": "comida", "nota": "prefere japonês ou pizza"})
    pizza = _entity(uid, "alimento", "Pizza", {"categoria": "fast_food"})
    cerveja = _entity(uid, "alimento", "Cerveja", {"categoria": "bebida", "alcoolica": True})

    # ── Trabalho Remoto — filhos ─────────────────────────────────
    rotina_trabalho = _entity(uid, "rotina", "Dia de Trabalho", {"hora_inicio": "09:00", "hora_fim": "18:00", "pausas": 3})
    ref_almoco = _entity(uid, "refeicao", "Almoço de Semana", {"horario": "12:30", "calorias": 550})
    pref_cafe = _entity(uid, "preferencia", "Café a cada 2h", {"tipo": "habito", "xícaras_dia": 4})
    arroz = _entity(uid, "alimento", "Arroz", {"categoria": "cereal"})
    frango = _entity(uid, "alimento", "Frango Grelhado", {"categoria": "proteina", "calorias_100g": 165})

    # ── Adicionar todas as entidades ─────────────────────────────
    entities = [
        perfil, saude, restricao, rotina,
        ctx_treino, ctx_sabado, ctx_trabalho,
        ref_pre, ref_pos, treino_peito, treino_costas, treino_corrida, supl_creatina, obj_hiper,
        banana, aveia, cafe, whey, leite_amend,
        ref_janta, pref_rodizio, pizza, cerveja,
        rotina_trabalho, ref_almoco, pref_cafe, arroz, frango,
    ]
    session.add_all(entities)
    session.flush()

    # ── Relações ─────────────────────────────────────────────────
    relations = [
        # Globais (direto no perfil do usuário)
        _relation(uid, perfil, saude, "TEM_ESTADO"),
        _relation(uid, perfil, restricao, "TEM_RESTRICAO"),
        _relation(uid, perfil, rotina, "TEM_ROTINA"),

        # Perfil → contextos
        _relation(uid, perfil, ctx_treino, "PARTICIPA"),
        _relation(uid, perfil, ctx_sabado, "PARTICIPA"),
        _relation(uid, perfil, ctx_trabalho, "PARTICIPA"),

        # Treino de Academia → filhos
        _relation(uid, ctx_treino, ref_pre, "CONTEM"),
        _relation(uid, ctx_treino, ref_pos, "CONTEM"),
        _relation(uid, ctx_treino, treino_peito, "CONTEM"),
        _relation(uid, ctx_treino, treino_costas, "CONTEM"),
        _relation(uid, ctx_treino, treino_corrida, "CONTEM"),
        _relation(uid, ctx_treino, supl_creatina, "CONTEM"),
        _relation(uid, ctx_treino, obj_hiper, "CONTEM"),

        # Pré-Treino → alimentos
        _relation(uid, ref_pre, banana, "CONTEM"),
        _relation(uid, ref_pre, aveia, "CONTEM"),
        _relation(uid, ref_pre, cafe, "CONTEM"),

        # Pós-Treino → alimentos
        _relation(uid, ref_pos, whey, "CONTEM"),
        _relation(uid, ref_pos, leite_amend, "CONTEM"),

        # Sábado à Noite → filhos
        _relation(uid, ctx_sabado, ref_janta, "CONTEM"),
        _relation(uid, ctx_sabado, pref_rodizio, "CONTEM"),
        _relation(uid, ref_janta, pizza, "CONTEM"),
        _relation(uid, ref_janta, cerveja, "CONTEM"),

        # Trabalho Remoto → filhos
        _relation(uid, ctx_trabalho, rotina_trabalho, "CONTEM"),
        _relation(uid, ctx_trabalho, ref_almoco, "CONTEM"),
        _relation(uid, ctx_trabalho, pref_cafe, "CONTEM"),
        _relation(uid, ref_almoco, arroz, "CONTEM"),
        _relation(uid, ref_almoco, frango, "CONTEM"),
    ]
    session.add_all(relations)
    session.commit()

    print(f"  [seed] {len(entities)} entidades + {len(relations)} relações criadas para 'lucas'")


def run() -> None:
    print("Seeding knowledge graph...")
    engine = create_engine(DATABASE_URL, echo=False)

    with Session(engine) as session:
        seed_knowledge_graph(session)

    print("Seed concluído.")


if __name__ == "__main__":
    run()
