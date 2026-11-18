"""`[[FILE:/workspace/…]]` sai da resposta (session-b4708de0, planilha dos estados).

O cartão `[[FILE:]]` é resolvido no Mac; caminho do sandbox vira sempre
"Não achei o arquivo", embaixo do botão de download que o `file_save` já pôs.
"""

import inspect

from modules.desktop import ai_service
from modules.desktop.ai_service import _arquivos_do_sandbox_sem_cartao, _ja_publicados

_RESPOSTA = (
    "Planilha pronta: 27 unidades federativas.\n\n"
    "Fonte: Wikipédia.\n\n"
    "[[FILE:/workspace/estados/estados_brasil_populacao_area.xlsx]]"
)


def test_tira_o_marcador_do_sandbox():
    novo, caminhos = _arquivos_do_sandbox_sem_cartao(_RESPOSTA)
    assert "[[FILE" not in novo
    assert novo == "Planilha pronta: 27 unidades federativas.\n\nFonte: Wikipédia."
    assert caminhos == ["/workspace/estados/estados_brasil_populacao_area.xlsx"]


def test_marcador_no_meio_da_linha_com_legenda_e_colchete_no_nome():
    texto = "aqui: [[FILE:/workspace/v/clipe [abc].mp4|o vídeo]] pronto"
    novo, caminhos = _arquivos_do_sandbox_sem_cartao(texto)
    assert novo == "aqui: pronto"
    assert caminhos == ["/workspace/v/clipe [abc].mp4"]


def test_arquivo_do_mac_fica():
    texto = "baixado: [[FILE:/Users/daniel/Downloads/taca-lê pau [nRuiacgrY0w].mp4]]"
    assert _arquivos_do_sandbox_sem_cartao(texto) == (texto, [])


def test_sem_marcador_volta_igual():
    assert _arquivos_do_sandbox_sem_cartao("nada aqui") == ("nada aqui", [])
    texto = "[[IMAGE:/workspace/x.png]]"
    assert _arquivos_do_sandbox_sem_cartao(texto) == (texto, [])


def test_ja_publicados_pelo_path_ou_pelo_nome():
    caminhos = ["/workspace/estados/a.xlsx", "/workspace/b.pdf", "/workspace/c.mp4"]
    produzidos = [
        {"file_id": "1", "name": "a.xlsx", "path": "/workspace/estados/a.xlsx"},
        {"file_id": "2", "name": "b.pdf"},
    ]
    assert _ja_publicados(caminhos, produzidos) == {"/workspace/estados/a.xlsx", "/workspace/b.pdf"}
    assert _ja_publicados(caminhos, None) == set()


def test_ligado_na_resposta_final_depois_das_imagens():
    src = inspect.getsource(ai_service)
    i = src.find("_imagens_do_sandbox_para_catalogo(\n            full_response, produced_files")
    j = src.find("_arquivos_do_sandbox_sem_cartao(full_response)")
    assert 0 < i < j


def test_marcador_com_file_id_sai():
    """session-b4708de0, turnos 7 e 8: `[[FILE:<file_id>]]` no lugar do caminho."""
    texto = "Aqui está o arquivo do Claude:\n\n[[FILE:fc94dbf1-3941-4bfd-af79-0e00a1470fc6]]"
    novo, caminhos = _arquivos_do_sandbox_sem_cartao(texto)
    assert novo == "Aqui está o arquivo do Claude:"
    assert caminhos == []
    novo, _ = _arquivos_do_sandbox_sem_cartao("ok [[FILE:file:4c2c0bc1-0a48-42c2-a2d8-db69adfa3ad0|planilha]]")
    assert novo == "ok"
