"""Ranking do PKS (docs/pks-ranking-plan.md) — funções puras, PKS sintético.

O PKS aqui imita a forma do caso real (plano → 5 treinos → exercícios, grupos
musculares, uma pessoa com muitas relações) sem copiar dado do usuário. Os casos
vêm das sessões d35696d4 e 098211a8:

- "lista por exercícios tb" (conversa sobre a semana) → os 5 dias, completos;
- "confere aí" depois de falar da quinta → a quinta, com os exercícios;
- "me mostra o treino de quarta" → só a quarta, sem folhas soltas ocupando vaga.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from core.pks.ranking import RankConfig, rank_pks, tokenize
from core.pks.schemas import EntityRecord, FieldRecord, RelationRecord

WS = uuid4()
NOW = datetime(2026, 9, 21, 20, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=100)


class _PKS:
    def __init__(self) -> None:
        self.entities: list[EntityRecord] = []
        self.fields: dict[UUID, list[FieldRecord]] = {}
        self.relations: list[RelationRecord] = []
        self.by_name: dict[str, EntityRecord] = {}

    def ent(self, name, etype, *, aliases=(), fields=None, updated=OLD, sensitivity="private") -> EntityRecord:
        e = EntityRecord(
            id=uuid4(), workspace_id=WS, name=name, type=etype, aliases=list(aliases),
            sensitivity=sensitivity, updated_at=updated,
        )
        self.entities.append(e)
        self.by_name[name] = e
        self.fields[e.id] = [
            FieldRecord(id=uuid4(), entity_id=e.id, key=k, value_type="text", value=v)
            for k, v in (fields or {}).items()
        ]
        return e

    def rel(self, a: str, rel: str, b: str) -> None:
        self.relations.append(RelationRecord(
            id=uuid4(), workspace_id=WS, from_entity_id=self.by_name[a].id,
            relation_type=rel, to_entity_id=self.by_name[b].id,
        ))

    def rank(self, message, *, keywords=None, history=None, **cfg):
        return rank_pks(
            entities=self.entities, fields_by_entity=self.fields, relations=self.relations,
            message=message, keywords=keywords, recent_messages=history,
            config=RankConfig(**cfg), now=NOW,
        )


DIAS = {
    "segunda": ("monday", "Peito e Tríceps", ["Supino Reto", "Supino Inclinado", "Crucifixo", "Tríceps na polia",
                                              "Tríceps costas na polia alta", "Tríceps francês crossover"]),
    "terça": ("tuesday", "Costas e Bíceps", ["Remada Alta com Barra", "Remada Sentado na Polia", "Rosca em Pé",
                                            "Rosca Alternada com Halteres", "Remada na Máquina Articulada",
                                            "Rosca Sentado no Banco"]),
    "quarta": ("wednesday", "Perna", ["Leg Press", "Leg Press Sentado", "Agachamento na Máquina em Pé",
                                      "Cadeira Extensora", "Cadeira Flexora", "Cadeira Abdutora", "Panturrilha em Pé"]),
    "quinta": ("thursday", "Ombro", ["Desenvolvimento na Máquina", "Elevação Lateral", "Elevação Frontal",
                                     "Puxada Baixa na Polia", "Travésio com Halteres", "Polia Unilateral de Ombro"]),
    "sexta": ("friday", "Peito e Bíceps", ["Supino Reto", "Supino Inclinado", "Crucifixo na Máquina", "Rosca em Pé",
                                           "Rosca Scott", "Martelo com Halteres"]),
}


@pytest.fixture
def pks() -> _PKS:
    p = _PKS()
    p.ent("Daniel Morais", "person", aliases=["Daniel", "eu", "me"])
    p.ent("Plano de treino de academia", "routine", aliases=["plano de treino", "treino ABCDE"],
          fields={"routine_type": "split"})
    p.rel("Daniel Morais", "works_on", "Plano de treino de academia")
    for dia, (weekday, label, exercicios) in DIAS.items():
        nome = f"Treino de {dia}-feira"
        # A quinta é a mais ANTIGA de propósito: no bug, ordem por updated_at a
        # deixava de fora.
        p.ent(nome, "workout", aliases=[dia, f"{dia}-feira"], fields={"weekday": weekday, "label": label},
              updated=OLD - timedelta(days=30) if dia == "quinta" else OLD)
        p.rel("Plano de treino de academia", "has_member", nome)
        for ex in exercicios:
            if ex not in p.by_name:
                # Exercícios recém-gravados: eram eles que ocupavam as vagas.
                p.ent(ex, "exercise", updated=NOW - timedelta(hours=1))
            p.rel(nome, "has_exercise", ex)
    for grupo, dias in {"Ombro": ["quinta"], "Perna": ["quarta"], "Peito": ["segunda", "sexta"]}.items():
        p.ent(grupo, "muscle_group", aliases=[grupo.lower() + "s"])
        for d in dias:
            p.rel(f"Treino de {d}-feira", "targets", grupo)
    # Barulho: um hub com muitas relações e coisas de outros assuntos.
    p.ent("PC Gamer RTX 5070 Ti", "device", aliases=["pc gamer"], fields={"gpu": "RTX 5070 Ti"})
    p.ent("MacBook Air M4", "device", aliases=["meu computador"])
    for i in range(30):
        p.ent(f"Documento {i}", "document")
        p.rel("Daniel Morais", "owns", f"Documento {i}")
    p.rel("Daniel Morais", "owns", "PC Gamer RTX 5070 Ti")
    return p


def _heads(outcome) -> list[str]:
    return [g.head.entity.name for g in outcome.groups]


# ── os três turnos das sessões ──────────────────────────────────────────────


def test_semana_inteira_com_a_conversa_traz_os_cinco_dias_completos(pks):
    history = [
        "agora falta so de quinta né",
        "Falta não — de quinta você já tem também. Ombro: Desenvolvimento na Máquina, Elevação Lateral",
        "tem mesmo! como ficou a semana completa",
        "A semana completa: Segunda Peito e Tríceps, Terça Costas e Bíceps, Quarta Perna, Quinta Ombro, Sexta Peito e Bíceps",
    ]
    out = pks.rank("lista por exercicios tb", keywords=["treino", "semana", "exercicios"], history=history)

    heads = _heads(out)
    for dia in DIAS:
        assert f"Treino de {dia}-feira" in heads, f"{dia} ficou de fora: {heads}"
    # Lista completa — o `[:8]` antigo cortava exercício da segunda e da terça.
    for dia, (_, _, exercicios) in DIAS.items():
        for ex in exercicios:
            assert ex in out.text, f"{ex} ({dia}) sumiu do bloco"
    # Exercício não ocupa vaga própria quando o treino dele está no bloco.
    assert not any(pks.by_name[h].type == "exercise" for h in heads)


def test_confere_ai_depois_de_falar_da_quinta_poe_a_quinta_primeiro(pks):
    history = [
        "Quinta — Ombro: Desenvolvimento na Máquina, Elevação Lateral, Elevação Frontal",
        "Só um aviso: o treino de quinta não apareceu na busca do PKS agora.",
    ]
    out = pks.rank("confere ai", keywords=["treino", "quinta", "ombro"], history=history)

    assert _heads(out)[0] == "Treino de quinta-feira"
    for ex in DIAS["quinta"][2]:
        assert ex in out.text


def test_treino_de_quarta_traz_so_a_quarta(pks):
    out = pks.rank("me mostra o treino de quarta", keywords=["treino", "quarta"])

    assert _heads(out) == ["Treino de quarta-feira"]
    for ex in DIAS["quarta"][2]:
        assert ex in out.text


# ── regras do plano ─────────────────────────────────────────────────────────


def test_folha_recente_nao_passa_na_frente_do_treino_antigo(pks):
    """D1: a quinta é a entidade mais velha e os exercícios os mais novos."""
    out = pks.rank("quais os treinos da semana", keywords=["treino", "semana"])
    assert "Treino de quinta-feira" in _heads(out)
    assert not any(pks.by_name[h].type == "exercise" for h in _heads(out))


def test_pergunta_sobre_exercicio_mostra_o_dia_que_tem_ele(pks):
    out = pks.rank("quantas series faco no leg press", keywords=["leg press"])
    assert _heads(out)[0] == "Treino de quarta-feira"
    assert out.decisions[pks.by_name["Leg Press"].id].startswith("agrupada em")


def test_erro_de_digitacao_ainda_acha(pks):
    out = pks.rank("e a pamturilha em pe?")
    assert "Treino de quarta-feira" in _heads(out)


def test_pergunta_sem_nada_do_pks_nao_monta_bloco(pks):
    out = pks.rank("quanto é 2+2?")
    assert out.text == ""
    assert out.groups == []


def test_hub_citado_nao_puxa_as_30_relacoes(pks):
    out = pks.rank("o que o Daniel tem de pc gamer", keywords=["pc gamer"])
    heads = _heads(out)
    assert "PC Gamer RTX 5070 Ti" in heads
    assert not any(h.startswith("Documento") for h in heads)


def test_orcamento_nunca_estoura_e_lista_nao_e_cortada_em_silencio(pks):
    def count(s: str) -> int:
        return len(s) // 4

    for budget in (40, 120, 300):
        out = rank_pks(
            entities=pks.entities, fields_by_entity=pks.fields, relations=pks.relations,
            message="lista os treinos da semana com exercicios", keywords=["treino", "semana"],
            config=RankConfig(budget_tokens=budget), count_tokens=count, now=NOW,
        )
        assert count(out.text) <= budget
        for g in out.groups:
            if g.level == "full":
                for ex in DIAS.get(g.head.entity.name.split(" ")[2].replace("-feira", ""), (None, None, []))[2]:
                    assert ex in g.text


def test_lista_grande_ganha_mais_n_explicito():
    p = _PKS()
    p.ent("Treino gigante", "workout", aliases=["gigante"])
    for i in range(25):
        p.ent(f"Exercício {i:02d}", "exercise")
        p.rel("Treino gigante", "has_exercise", f"Exercício {i:02d}")
    out = p.rank("me mostra o treino gigante", list_cap=20)
    assert "+5" in out.text
    assert "search_pks" in out.text


def test_sensivel_so_entra_com_mencao_direta():
    p = _PKS()
    p.ent("Exame de sangue", "health_record", sensitivity="sensitive", fields={"resultado": "x"})
    p.ent("Consulta médica", "event", fields={"obs": "exame de sangue marcado"})
    assert "Exame de sangue" not in _heads(p.rank("tenho consulta médica quando?"))
    assert "Exame de sangue" in _heads(p.rank("qual o resultado do exame de sangue"))


def test_restricao_de_saude_entra_sempre():
    p = _PKS()
    p.ent("Alergia a amendoim", "restriction")
    p.ent("PC Gamer", "device")
    out = p.rank("quanto é 2+2?")
    assert _heads(out) == ["Alergia a amendoim"]


def test_extrato_nunca_entra():
    p = _PKS()
    p.ent("Saldo Nubank", "account", fields={"balance": "980"})
    out = p.rank("qual meu saldo no nubank")
    assert "Saldo Nubank" not in _heads(out)


def test_explain_tem_os_sinais_e_a_decisao(pks):
    out = pks.rank("me mostra o treino de quarta", keywords=["treino", "quarta"])
    row = out.explain(3)[0]
    assert row["entity"] == "Treino de quarta-feira"
    for key in ("nome", "lex", "conv", "grafo", "tipo", "prior", "score", "decision"):
        assert key in row
    assert row["decision"] == "entrou"


def test_tokenize_plural_acento_e_dia_em_ingles():
    assert tokenize("Exercícios de Ombros") == ["exercicio", "ombro"]
    assert tokenize("quinta-feira") == ["quinta", "feira"]
    assert tokenize("weekday: thursday") == ["weekday", "quinta"]


# ── ajustes que o gabarito pediu (bench de 2026-09-21) ──────────────────────


def test_caminho_de_arquivo_nao_vira_consulta(pks):
    """"/Users/daniel/Downloads/…" trazia "daniel" e o usuário inteiro entrava."""
    pks.ent("NFSE_novembro.pdf", "document", aliases=["NFSe novembro"])
    out = pks.rank("resume /Users/daniel/Downloads/NFSE_novembro.pdf pra mim")
    heads = _heads(out)
    assert heads[0] == "NFSE_novembro.pdf"
    assert "Daniel Morais" not in heads


def test_frase_maior_de_outra_entidade_sombreia_o_primeiro_nome(pks):
    pks.ent("Daniel Ferreira", "person", aliases=["ferreira"])
    out = pks.rank("o que vc sabe sobre o daniel ferreira?")
    assert _heads(out)[0] == "Daniel Ferreira"
    assert "Daniel Morais" not in _heads(out)


def test_pre_treino_nao_e_pedido_de_treino(pks):
    pks.ent("Bebida pré-treino", "preference", aliases=["bebida pre-treino", "pre treino"],
            fields={"recipe": "suco de uva com água com gás"})
    out = pks.rank("como era a bebida de pre treino mesmo?")
    assert _heads(out)[0] == "Bebida pré-treino"
    assert "tipo" not in out.asked_types and "workout" not in out.asked_types


def test_apelido_generico_nao_vira_nome(pks):
    """"Dia C" (apelido da quarta) casava com "5 dias" e derrubava os outros dias."""
    pks.by_name["Treino de quarta-feira"].aliases.append("Dia C")
    out = pks.rank("me devolve meu treino completo dos 5 dias", keywords=["treino", "dias"])
    for dia in DIAS:
        assert f"Treino de {dia}-feira" in _heads(out)


def test_substantivo_do_tipo_casa_com_todos_do_tipo(pks):
    pks.by_name["MacBook Air M4"].aliases.append("esse dispositivo")
    out = pks.rank("quais os meus dispositivos?", keywords=["dispositivo"])
    assert {"PC Gamer RTX 5070 Ti", "MacBook Air M4"} <= set(_heads(out))


def test_pergunta_de_dinheiro_nao_monta_bloco(pks):
    out = pks.rank("qual o valor da fatura de agosto", history=["Quarta — Perna: Leg Press, Cadeira Extensora"])
    assert out.groups == []


def test_desempate_nao_decide_se_entra(pks):
    """O prior ordena, mas não decide: a quarta, citada DUAS falas atrás, soma
    abaixo do limiar sem o prior e acima com ele — e tem que ficar de fora.

    (Citada na fala IMEDIATAMENTE anterior ela entra, mesmo que a mensagem de
    agora seja "2+2": o ranking não distingue troca de assunto de anáfora como
    "confere aí". Custo conhecido e limitado; os negativos reais do gabarito
    passam.)"""
    # Gravada agora: desempate no máximo, que é o cenário que a regra protege.
    pks.by_name["Treino de quarta-feira"].updated_at = NOW
    out = pks.rank("quanto é dois mais dois?", history=["tem um treino de perna na quarta", "ok"])
    quarta = next(r for r in out.ranked if r.entity.name == "Treino de quarta-feira")
    sem_prior = quarta.score - 0.05 * quarta.signals["prior"]
    assert sem_prior < 0.12 <= quarta.score, (sem_prior, quarta.score)
    assert out.groups == []


def test_hub_nao_casa_pelos_nomes_dos_vizinhos(pks):
    out = pks.rank("me fala do documento 7")
    assert "Daniel Morais" not in _heads(out)


def test_entidade_grande_entra_compacta():
    p = _PKS()
    p.ent("Dossiê enorme", "document", aliases=["dossie"],
          fields={f"campo_{i}": "texto longo " * 30 for i in range(8)})
    out = p.rank("abre o dossie", entity_token_cap=120)
    assert out.groups[0].level == "compact"


# ── orçamento elástico (docs/pks-ranking-plan.md §12) ───────────────────────


def _count(s: str) -> int:
    return len(s) // 4


def test_grupo_forte_usa_a_extensao_e_a_cauda_nao(pks):
    """A semana inteira não cabe na base; com a extensão, os dias FORTES entram
    todos — e só eles."""
    history = ["A semana: Segunda Peito, Terça Costas, Quarta Perna, Quinta Ombro, Sexta Peito e Bíceps"]
    kw = ["treino", "semana"]
    so_base = rank_pks(
        entities=pks.entities, fields_by_entity=pks.fields, relations=pks.relations,
        message="lista os treinos da semana", keywords=kw, recent_messages=history,
        config=RankConfig(budget_tokens=150), count_tokens=_count, now=NOW,
    )
    elastico = rank_pks(
        entities=pks.entities, fields_by_entity=pks.fields, relations=pks.relations,
        message="lista os treinos da semana", keywords=kw, recent_messages=history,
        config=RankConfig(budget_tokens=150, budget_max_tokens=2000, strong_ratio=0.7),
        count_tokens=_count, now=NOW,
    )
    assert len(elastico.groups) > len(so_base.groups)
    top = max(g.score for g in elastico.groups)
    estendidos = [eid for eid, d in elastico.decisions.items() if "orçamento estendido" in d]
    assert estendidos, "nenhum grupo usou a extensão"
    by_group = {g.head.entity.id: g for g in elastico.groups}
    for eid in estendidos:
        assert by_group[eid].score >= 0.7 * top, "cauda fraca não pode usar a extensão"
    assert _count(elastico.text) <= 2000


def test_sem_teto_nao_ha_extensao(pks):
    out = rank_pks(
        entities=pks.entities, fields_by_entity=pks.fields, relations=pks.relations,
        message="lista os treinos da semana", keywords=["treino", "semana"],
        config=RankConfig(budget_tokens=150), count_tokens=_count, now=NOW,
    )
    assert _count(out.text) <= 150
    assert not any("estendido" in d for d in out.decisions.values())
