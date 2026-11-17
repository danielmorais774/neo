"""Guarda anti-alegação-de-download: corpus rotulado a partir de prod.

O predicado era um regex único com `\\bdownload\\b` solto. Medido contra as 1450
mensagens de assistant do banco de produção, dava 20 disparos: ~7 falsos
positivos E 11 alegações reais NÃO pegas — ruim nos dois eixos.

O FP mais caro derrubava o turno inteiro: a resposta certa
`[[IMAGE:/Users/daniel/Downloads/download.png]]` casava pela palavra dentro do
CAMINHO, a guarda forçava `tool_choice="required"` e o DeepSeek em thinking mode
respondia 400 (sessão 1aec71b8). Na prática: pedir qualquer coisa da pasta
Downloads quebrava o turno.

Os casos marcados (prod) são transcrições literais desses disparos reais.
"""

from modules.desktop.ai_service import _claims_downloadable_delivery as claims

# ── NÃO pode disparar ───────────────────────────────────────────────────────
NAO_E_ALEGACAO = [
    # (prod) a palavra só existe dentro de caminho/nome de arquivo
    "Essa aqui:\n\n[[IMAGE:/Users/daniel/Downloads/download.png]]",
    "Achei o arquivo. Tá aí:\n\n[[IMAGE:/Users/daniel/Downloads/foto.png]]",
    "Claro! Encontrei **20 arquivos de imagem** na sua pasta de Downloads. "
    "Aqui estão eles:\n\n1. `download (1).png`\n2. `IMG_20260601_110121.jpg`",
    "Seu diretório de downloads tem 41 PDFs. O maior é `DANFE.pdf`, com 2,1 MB.",
    "Movi `relatorio.pdf` da pasta Downloads pra sua área de trabalho.",
    # (prod) negação / relato de falha — o oposto de uma alegação
    "Não dá para gerar nem colar um link direto de download aqui no chat. "
    "O sistema guarda os arquivos no painel do Neo.",
    "Não consegui executar `save_generated_file`. Erro: Download falhou: 404 Not Found",
    "Não tem botão de download nenhum porque eu não cheguei a salvar o arquivo.",
    "Não consegui deixar o arquivo pronto pra baixar — o file_save falhou.",
    # (prod) negação DENTRO do trecho que casa — não pode escapar da polaridade
    "Os arquivos do site já estão prontos. Só não consegui baixar as imagens "
    "automaticamente por causa de bloqueio de acesso.",
    # (prod) baixar da INTERNET, não entrega nossa
    "Falhou por bloqueio de acesso (erro 403 Forbidden). Isso acontece quando o "
    "servidor não permite download via comando direto (wget) pelo meu ambiente.",
    "Você pode baixar essas imagens manualmente em cdn.pixabay.com (procure por gato).",
    "O `wget` baixou o arquivo do servidor pra dentro do meu sandbox, mas o processamento falhou.",
    # (prod) gíria — "pode baixar" sem nenhuma superfície de entrega ancorando
    "A piada é toda construída pra expor quem responde distraído. "
    "Pode baixar a placa, campeão, aqui não tem vacilo.",
    # oferta/pergunta, não afirmação
    "Quer que eu gere o arquivo pra download ou prefere só copiar?",
    "Ainda não gerei o arquivo. Quer que eu monte o CSV pra você baixar?",
    "Você quer que eu salve isso num arquivo pra download, ou prefere ver aqui na conversa mesmo?",
    # (prod) promessa futura — território do _promises_pending_action
    "Estruturei o projeto e criei a pasta para as imagens. Assim que estiver com "
    "tudo pronto, vou zipar e te entrego o link pra baixar.",
    # literais dentro de código
    "Rodei o comando:\n\n```bash\ncurl -o download.zip https://exemplo.com/pacote.zip\n```\n\nDeu 404.",
    "O link https://site.com/download/v2 está fora do ar.",
    # conteúdo entregue INLINE, sem prometer botão nenhum
    "Segue seu extrato de 01/07 a 22/07 no formato CSV:\n\ndata,valor,descricao\n15/07,-1060.00,Adiantamento",
]

# ── TEM que disparar ────────────────────────────────────────────────────────
E_ALEGACAO = [
    # (prod) o caso que motivou a guarda — sessão 95ccd52d
    "Arquivo CSV das suas finanças está pronto. Você pode baixar pelo botão abaixo.",
    # (prod) "pronto/disponível … para download"
    "Pronto. Seu arquivo extrato_2026_07.csv está gerado e disponível para download. Só baixar aí.",
    "Arquivo financas_julho_2026.csv criado e já disponível pra download aqui na conversa.",
    "Arquivo extrato-financas-jul-2026.xlsx pronto. Já está disponível para download aí na interface.",
    "Terminei: o .zip está disponível pra download logo abaixo.",
    # (prod) botão
    "Arquivo portifolio_site.zip criado e pronto pra baixar. Só clicar no botão de "
    "download que apareceu aí embaixo.",
    "Arquivo financas_julho_2026.csv gerado e guardado. Assim que a interface mostrar, "
    "o botão de download vai aparecer pra você baixar direto.",
    "Arquivo primeiros_pares.zip tá salvo. Pode baixar aí pelo botão da interface.",
    # (prod) alegações que o regex ANTIGO não pegava
    "Pronto. O arquivo `fibonacci.zip` está salvo no catálogo. Pode baixar pelo link.",
    "O arquivo fibonacci_100.zip já está gerado e guardado. É só baixar na aba Arquivos do painel.",
    "Não rola link direto pelo chat por segurança. Para baixar, vai na aba Arquivos "
    "do painel — o arquivo já está lá.",
    "Arquivo zip do site do Naruto está pronto e já pode ser baixado aí na interface.",
    "Pronto. O extrato em XLSX já foi salvo e pode ser baixado direto da sua área de trabalho.",
    # (prod) link markdown pro sandbox: falso por construção
    "Arquivo gerado: extrato_2026_07.csv.\n\nSó baixar aqui:\n\n"
    "[extrato_2026_07.csv](sandbox:/workspace/extrato_2026_07.csv)",
    # anexo
    "Prontinho, o relatório está aí em anexo — é só clicar pra baixar.",
    "Segue o arquivo em anexo.",
    "Gerei o PDF. Ele já aparece aqui na conversa pra você baixar.",
    # sem citar o nome do artefato: a alegação é a mesma (o objeto veio do turno
    # anterior). Uma camada "menciona artefato?" barrava isto — por isso saiu.
    "Está pronto, pode baixar pelo botão abaixo.",
]


def test_nao_dispara_sem_alegacao():
    for texto in NAO_E_ALEGACAO:
        assert claims(texto) is False, f"falso positivo em: {texto[:90]!r}"


def test_dispara_em_alegacao_real():
    for texto in E_ALEGACAO:
        assert claims(texto) is True, f"falso negativo em: {texto[:90]!r}"


def test_mascara_nao_come_prosa():
    """A máscara só pode apagar literal — se comer prosa, a guarda morre calada.

    Uma versão intermediária aceitava `/` solto como início de caminho: em
    "julho/2026 está pronto. Você pode baixar pelo botão abaixo." ela engolia da
    barra até o fim e a alegação sumia. Esse é o modo de falha mais perigoso da
    camada de máscara, porque não aparece como erro — só como guarda inerte.
    """
    from modules.desktop.ai_service import _prose_only

    texto = "Arquivo CSV de julho/2026 está pronto. Você pode baixar pelo botão abaixo."
    assert "pode baixar pelo botão abaixo" in _prose_only(texto)
    assert claims(texto) is True

    # ...mas caminho de verdade continua sendo apagado
    assert "/Users/daniel/Downloads" not in _prose_only("olha em /Users/daniel/Downloads")


def test_negacao_condicional_nao_invalida_alegacao():
    """"se não aparecer" é condicional, não desmentido — a alegação continua.

    As duas frases têm "não" antes do trecho que casa; só a segunda desmente.
    Sem essa distinção a guarda perderia toda alegação hedgeada, que é
    justamente como o modelo fala quando não tem certeza de ter entregue.
    """
    assert claims("Se não aparecer o botão de download do arquivo, me avisa.") is True
    assert claims(
        "Se não aparecer pra download na interface, vai no painel — o arquivo já está lá."
    ) is True
    assert claims("Não tem botão de download porque eu não salvei o arquivo.") is False
