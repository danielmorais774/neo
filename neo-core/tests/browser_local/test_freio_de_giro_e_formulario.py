"""O freio de giro contra um FORMULÁRIO — job 683fa0d8 (2026-10-07).

O job preencheu CEP, preencheu CPF, clicou em "Calcular frete" e morreu
`failed` com a cotação já renderizada na tela ("Frete: R$ 24,90"). Dois
defeitos somados, e os dois ficam travados aqui:

1. ``_fingerprint`` não incluía o VALOR dos inputs. Digitar num campo de texto
   não mexe em url, título, rótulo nem texto da página, então cada `fill` saía
   carimbado "a tela NÃO mudou" — é a terceira vez que esta assinatura precisa
   aprender um estado invisível (antes foram a seleção de rádio e o painel
   aberto, ambos com comentário no código).
2. ``_motivo_para_parar`` media o giro pelo ``max()`` de TODAS as telas já
   vistas. Com isso, progresso não limpava nada: a tela do formulário vazio
   acumulou 3, o submit finalmente mudou a página, e o 3 velho continuou sendo
   o máximo — o job parou no passo seguinte ao único que funcionou.

O teste de giro de verdade fica junto de propósito: o conserto não pode ter
desligado a rede que o freio existe para ser.
"""

from __future__ import annotations

from core.browser_local.driver import BrowserLocalDriver


def _driver() -> BrowserLocalDriver:
    return BrowserLocalDriver(act=None, llm=None, model="x")


def _frete(cep: str = "", cpf: str = "", resultado: bool = False) -> dict:
    """A fixture do §24 como o snapshot do driver a entrega."""
    texto = "Cortina Blackout 2,00 x 1,80 m ... Calcular frete"
    if resultado:
        texto += " Cotação simulada. Frete: R$ 24,90 — entrega em 6 dias úteis."
    return {
        "url": "http://localhost:8099/frete.html",
        "title": "Cortinas Boa Vista — calcular frete",
        "text": texto,
        "elements": [
            {"ref": 0, "tag": "label", "text": "CEP de entrega", "controla": "input[cep]", "value": cep},
            {"ref": 1, "tag": "input", "text": "00000-000", "value": cep},
            {"ref": 2, "tag": "label", "text": "CPF do destinatário", "controla": "input[cpf]", "value": cpf},
            {"ref": 3, "tag": "input", "text": "000.000.000-00", "value": cpf},
            {"ref": 4, "tag": "button", "type": "submit", "text": "Calcular frete"},
        ],
    }


def test_preencher_um_campo_muda_a_assinatura_da_tela():
    d = _driver()
    vazio = d._fingerprint(_frete())
    com_cep = d._fingerprint(_frete(cep="62500-000"))
    com_os_dois = d._fingerprint(_frete(cep="62500-000", cpf="11144477735"))
    assert vazio != com_cep != com_os_dois
    assert vazio != com_os_dois


def test_o_job_que_morreu_agora_atravessa_os_quatro_passos():
    d = _driver()
    passos = [
        _frete(),
        _frete(cep="62500-000"),
        _frete(cep="62500-000", cpf="11144477735"),
        _frete(cep="62500-000", cpf="11144477735", resultado=True),
    ]
    for i, snap in enumerate(passos):
        d._note_change(snap)
        d._contabilizar_producao(snap)
        if i:
            d._acting_steps += 1
        assert d._motivo_para_parar() == "", f"parou no passo {i + 1}, com progresso real"
    # Quatro telas distintas, cada uma vista uma vez. Antes: uma só, vista 4x.
    assert len(d._telas_vistas) == 4
    assert set(d._telas_vistas.values()) == {1}


def test_giro_de_verdade_continua_sendo_cortado():
    """Mesma tela, nada mudando, caderno vazio — o caso para o qual o freio existe."""
    d = _driver()
    parada = {
        "url": "https://exemplo.test/a",
        "title": "A",
        "text": "nada muda aqui",
        "elements": [{"ref": 0, "tag": "button", "text": "Tentar de novo"}],
    }
    motivos = []
    for _ in range(3):
        d._note_change(parada)
        d._contabilizar_producao(parada)
        d._acting_steps += 1
        motivos.append(d._motivo_para_parar())
    assert motivos[0] == "" and motivos[1] == ""
    assert "giro, não progresso" in motivos[2]


def test_tela_revisitada_com_caderno_cheio_nao_e_giro():
    """Abrir um detalhe e voltar é navegação normal quando já se produziu algo."""
    d = _driver()
    parada = {
        "url": "https://exemplo.test/a",
        "title": "A",
        "text": "lista",
        "elements": [{"ref": 0, "tag": "a", "text": "item"}],
    }
    d.notes.append("achei o preço: R$ 10")
    for _ in range(4):
        d._note_change(parada)
        d._contabilizar_producao(parada)
        d._acting_steps += 1
    assert d._motivo_para_parar() == ""
