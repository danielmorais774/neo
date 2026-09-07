"""Frases de referência do portão — sem dependência de pytest.

Vive fora do arquivo de teste porque o `scripts/eval_browser_gate.py` também
importa daqui, e ele roda dentro do container de produção, onde pytest não
está instalado.
"""

PEDIU = [
    "no meu navegador, entra no portal da NFSe e baixa a última nota",
    "abre o github no meu navegador e me diz o título da página",
    "faz isso no navegador local",
    "quero acompanhar no chrome a busca de passagens",
    "no meu Brave, entra na Amazon e procura teclado",
    "na minha tela, abre o youtube",
    "roda no browser local aí",
    "quero ver você fazendo isso no navegador",
    "vê as abas abertas aqui",
    "abre no meu chrome e deixa eu ver",
    # Como o usuário escreveu de verdade (sessão d01cb5ab).
    "abra o painel do simple nacional no meu local",
    "usa a extensao do meu navegador",
    # Sessão d3be77e1: o pedido mais explícito da conversa toda foi rebaixado
    # para o headless por causa de UM erro de digitação em "navegador". Quem
    # digita rápido troca letra; a guarda não pode depender de ortografia.
    "essa o google drive pelo meu nevagador local e baixa o app-relase.apk via extensao",
    "abre no meu nevagador",
    "abre no meu navegdor",       # letra faltando
    "abre no meu navegaador",     # letra sobrando
    "abre no meu chorme",         # transposição em palavra curta
    # Extensão de navegador vale sozinha: o headless do servidor não tem uma.
    "Assim nao, usa a extensao",
    "nao mesmo, tente de novo usando a extensao",
    # A MÁQUINA dele, nomeada, vale sozinha — o servidor não é ela. Medido: em 33
    # decisões no log o portão PROMOVEU zero vezes, e estas frases reais eram
    # justamente as que caíam no headless (jobs 3a0ed590 e cfeeb987).
    "abre uma aba no meu navegador com uma playlist de rock e coloca para tocar no meu macbook",
    "coloca para tocar no meu macbook",
    "consegue por um musica nesse pc",
    "toca um rock no meu computador",
    "roda isso na minha maquina",
    "abre o youtube nesse notebook",
    "faz isso no meu novo macbook",
]

NAO_PEDIU = [
    "procura passagens para São Paulo",
    "abre meu email e vê se chegou algo",
    "pesquisa no google o preço do teclado",
    "entra no site do banco e vê meu saldo",
    "minha conta no mercado livre está com problema",
    "busca no navegador quanto custa um monitor",   # afordância sem âncora
    "meu chefe pediu um relatório de preços",       # âncora sem afordância
    "faz uma pesquisa profunda sobre isso",
    "abre o site da receita e vê o status do CPF",
    "quero saber o preço do dólar hoje",
    "minha internet está lenta, testa a velocidade",
    "vê se o site está no ar",
    "vou ao local do evento amanhã",                # "local" sozinho não conta
    "o restaurante local é bom?",
    "comprei no mercado local",
    # "extensão" tem outro sentido em pt-BR, e nele o navegador é o alvo errado.
    "qual a extensao desse arquivo",
    "qual a extensao do arquivo que você baixou",
    "me diz a extensao daquele video",
    "salva com extensao .csv",
    # A tolerância a erro de digitação não pode inventar navegador onde não há:
    # a uma edição de "brave" moram "grave", "bravo" e "breve".
    "meu problema é grave",
    "meu bravo amigo chegou",
    "meu negócio está travado",
    # "navegador" aqui é cabeçalho HTTP, não a tela dele (mensagem real do banco).
    "baixe 3 imagens livres com curl usando user-agent de navegador",
    # A máquina só conta com POSSESSIVO e no sentido de computador: sem isso,
    # falar sobre um PC vira pedido de tela.
    "o pc esta lento hoje",
    "qual o melhor macbook de 2026?",
    "compra um computador novo pra mim",
    "quanto custa um notebook bom",
    # "máquina" tem outro sentido, e nele o navegador é o alvo errado.
    "minha maquina de lavar quebrou",
    "minha maquina de cafe parou de funcionar",
]


# ── tocar mídia (sessão 0939a295) ───────────────────────────────────────────
#
# Predicado SEPARADO de PEDIU/NAO_PEDIU: aqui a pergunta não é "ele pediu o
# navegador dele", é "isto é reprodução?". Play num headless de VPS nunca
# funciona, então a resposta positiva manda para o navegador do usuário mesmo
# sem nenhuma palavra de navegador na frase.

TOCA_MIDIA = [
    # A frase real do turno que abriu o bug, e a tarefa que o cérebro escreveu.
    "to de boa, coloca uma musica do meu mix para tocar ai",
    "Abrir a URL https://www.youtube.com/watch?v=c-g6X5nsbn8 e dar play no "
    "vídeo. É um mix/radio de rock do YouTube.",
    # Jobs de servidor que já nasceram mortos por isto.
    "Tocar rock no MacBook",
    "abre uma aba no meu navegador com uma playlist de rock e coloca para tocar",
    "consegue tocar uma musica",
    "poe uma musica pra tocar",
    "reproduzir o podcast do Flow",
    "toca a playlist do spotify",
    # Frase comprida que manda tocar E manda não encostar em nada.
    "dar play no video e nao toque no botao de anuncio",
]

NAO_TOCA_MIDIA = [
    # Navegação: "tocar" no sentido de encostar. Se contasse como play, uma
    # compra guiada sequestraria o navegador do usuário.
    "nao toque no botao de comprar",
    "sem tocar em nada, so lista os precos do video",
    "abre o youtube e nao toque em nada",
    "abre o spotify, toca no item da playlist e me diz o nome",
    # Ler metadado de mídia o headless faz bem — só a REPRODUÇÃO não existe lá.
    "ve quantas visualizacoes tem o video no youtube",
    "procura a letra da musica X",
    "qual o nome da musica que toca no comercial",
    # "da **play**list" não é "dar play": `_has_word` casa expressão por
    # substring, e com isso copiar um link virava ordem de tocar.
    "abre a pagina da playlist e me diz quantas faixas tem",
    "copia o link da playlist",
    "me manda o nome da playlist do spotify",
    # Nada a ver com mídia.
    "acessa a amazon e acha uma promocao de iphone",
    "abre meu email e ve se chegou algo",
]
