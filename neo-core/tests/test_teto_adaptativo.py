"""core/llm/teto_adaptativo — o teto acompanha o raciocínio sem cortar nem inflar."""

from __future__ import annotations

import pytest

from core.llm.teto_adaptativo import Leitura, TetoAdaptativo, ler_resposta
from shared import ajustes as mod

#: (raciocínio, completion) de cada passo do job 9c9b5bae, em ordem, medidos no
#: /generation do OpenRouter. Os pares (≥1984, 2000) foram os seis cortes.
SERIE_9C9B5BAE = [
    (192, 271), (1984, 2000), (175, 370), (1519, 1609), (864, 1118), (985, 1175),
    (2000, 2000), (1174, 1439), (50, 114), (82, 222), (64, 116), (453, 670),
    (686, 760), (567, 804), (2000, 2000), (346, 397), (810, 996), (2000, 2000),
    (526, 754), (29, 79), (32, 123), (42, 92), (386, 503), (169, 270),
    (428, 598), (1288, 1367), (1566, 1743), (2000, 2000), (2000, 2000), (1492, 1848),
]


@pytest.fixture(autouse=True)
def _padroes(tmp_path, monkeypatch):
    """Os testes valem para os PADRÕES, não para o neo.toml de quem roda."""
    monkeypatch.setenv("NEO_AJUSTES", str(tmp_path / "nao-existe.toml"))
    monkeypatch.setattr(mod, "_cache", None)


def _teto() -> TetoAdaptativo:
    return TetoAdaptativo("navegador.decisao")


def test_comeca_no_piso():
    assert _teto().teto("glm") == 2000


def test_corte_por_raciocinio_e_o_que_invadiu_a_reserva_da_resposta():
    t = _teto()
    # Job 9c9b5bae, passo 2: 1984 de 2000 pensando, 60 caracteres de JSON.
    assert t.corte_por_raciocinio(Leitura(cortado=True, raciocinio=1984), 2000)
    # Cortado com raciocínio baixo: quem cresceu foi a RESPOSTA.
    assert not t.corte_por_raciocinio(Leitura(cortado=True, raciocinio=300), 2000)
    # Raciocínio alto mas fechou: não houve corte.
    assert not t.corte_por_raciocinio(Leitura(cortado=False, raciocinio=1984), 2000)
    # Provedor que não diz: não dá para afirmar.
    assert not t.corte_por_raciocinio(Leitura(cortado=True, raciocinio=None), 2000)


def test_escada_dobra_ate_o_maximo():
    t = _teto()
    assert t.escada(2000) == 4000
    assert t.escada(4000) == 6000
    assert t.escada(6000) is None


def test_sem_contagem_de_raciocinio_nao_aprende():
    t = _teto()
    t.observar("glm", Leitura(cortado=False, raciocinio=None), 2000)
    assert t.teto("glm") == 2000


def test_cada_modelo_aprende_sozinho():
    t = _teto()
    t.observar("glm", Leitura(cortado=False, raciocinio=1500), 2000)
    assert t.teto("glm") > 2000
    assert t.teto("outro") == 2000


def test_passos_leves_nao_derrubam_o_teto_abaixo_do_piso():
    t = _teto()
    for _ in range(50):
        t.observar("glm", Leitura(cortado=False, raciocinio=30), 2000)
    assert t.teto("glm") == 2000


def test_raciocinio_desembestado_para_no_maximo():
    t = _teto()
    for _ in range(10):
        t.observar("glm", Leitura(cortado=True, raciocinio=6000), 6000)
    assert t.teto("glm") == 6000


def test_serie_real_do_job_9c9b5bae():
    """Reencena os 30 passos com o teto aprendido.

    Nenhum dos 24 passos que fecharam com 2000 passa a cortar, e nos seis que
    cortaram o teto já está acima do que se mediu (o valor real deles é
    desconhecido: ≥ 2000) — exceto o primeiro, no começo frio, que é o caso da
    escada.
    """
    t = _teto()
    tetos = []
    for raciocinio, completion in SERIE_9C9B5BAE:
        teto = t.teto("glm")
        tetos.append(teto)
        era_corte = completion == 2000 and raciocinio >= 1984
        if era_corte:
            leitura = Leitura(cortado=teto <= 2000, raciocinio=raciocinio)
        else:
            assert completion <= teto
            leitura = Leitura(cortado=False, raciocinio=raciocinio)
        t.observar("glm", leitura, teto)
    cortes = [i for i, (r, c) in enumerate(SERIE_9C9B5BAE) if c == 2000 and r >= 1984]
    assert tetos[cortes[0]] == 2000  # começo frio: a escada cobre
    assert all(tetos[i] > 2000 for i in cortes[1:])
    assert max(tetos) <= 6000


class _Resp:
    def __init__(self, motivo=None, raciocinio=None, *, como_atributo=False):
        if como_atributo:
            self.finish_reason = motivo
            self.reasoning_tokens = raciocinio
        else:
            self._m, self._r = motivo, raciocinio

    def get_content(self):
        return "{}"


def test_ler_resposta_aceita_metodo_atributo_ou_nada():
    class ComMetodo:
        def finish_reason(self):
            return "length"

        def reasoning_tokens(self):
            return 1984

    assert ler_resposta(ComMetodo()) == Leitura(cortado=True, raciocinio=1984)
    assert ler_resposta(_Resp("stop", 10, como_atributo=True)) == Leitura(cortado=False, raciocinio=10)
    # Dublê sem nenhum dos dois (os do tests/browser_local): "não sei".
    assert ler_resposta(_Resp()) == Leitura(cortado=None, raciocinio=None)
