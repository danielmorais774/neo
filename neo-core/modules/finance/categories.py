"""Seed de categorias padrão (2 níveis) para o contexto brasileiro.

Inserido por usuário na primeira chamada a qualquer tool de finanças (lazy),
com ``is_system=true``. O usuário pode criar/arquivar as suas via ``fin_manage``.
"""

from __future__ import annotations

# (pai, [filhas]) — kind despesa
DEFAULT_EXPENSE_CATEGORIES: list[tuple[str, list[str]]] = [
    ("Alimentação", ["Mercado", "Restaurante", "Delivery", "Lanche/Café"]),
    ("Transporte", [
        "Combustível", "App de transporte", "Transporte público",
        "Manutenção do veículo", "Estacionamento/Pedágio",
    ]),
    ("Moradia", [
        "Aluguel", "Condomínio", "Energia", "Água", "Internet", "Gás",
        "Manutenção da casa",
    ]),
    ("Saúde", ["Farmácia", "Plano de saúde", "Consultas/Exames", "Academia"]),
    ("Educação", ["Cursos", "Livros", "Mensalidade"]),
    ("Lazer", ["Streaming", "Jogos", "Passeios", "Bares/Festas"]),
    ("Assinaturas e serviços", ["Celular", "Apps/Serviços digitais"]),
    ("Compras", ["Roupas", "Eletrônicos", "Casa/Decoração", "Presentes"]),
    ("Pets", []),
    ("Viagem", []),
    ("Cuidados pessoais", ["Barbearia/Salão", "Cosméticos"]),
    ("Impostos e taxas", ["Tarifas bancárias", "Juros/Multas", "Impostos"]),
    ("Doações", []),
    ("Outros gastos", []),
]

# (pai, [filhas]) — kind receita
DEFAULT_INCOME_CATEGORIES: list[tuple[str, list[str]]] = [
    ("Salário", []),
    ("Renda extra", ["Freelas", "Vendas"]),
    ("Rendimentos", []),
    ("Reembolso", []),
    ("Presente/Prêmio", []),
    ("Outras receitas", []),
]
