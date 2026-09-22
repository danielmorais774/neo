"""Tools do File Store expostas ao cognitivo (docs/file-store-plan.md §4).

Prefixo ``file_`` = CATÁLOGO de arquivos (o que o usuário te mandou, o que você
guardou). Não confundir com ``neo_read_file``/``neo_write_file``, que são o
RASCUNHO do seu sandbox (/workspace). A distinção está escrita em cada
description de propósito (§14.5): o modelo erra se ela ficar implícita.

Economia de tokens é o princípio (§5): o conteúdo NUNCA entra inteiro no
contexto — o prompt recebe só o manifesto e o modelo lê em fatias.
"""

from shared import ToolParameter, ToolSchema

FILE_TOOLS: list[ToolSchema] = [
    ToolSchema(
        name="file_list",
        description=(
            "Lista o CATÁLOGO de arquivos do Neo (o que o usuário enviou, o que você "
            "guardou). Use quando o usuário citar um arquivo sem anexar agora ('aquele "
            "CSV que te mandei ontem'). NÃO é o seu sandbox — para o rascunho em "
            "/workspace use neo_list_files."
        ),
        module="files",
        execution="server-side",
        timeout=15,
        parameters={
            "retention": ToolParameter(
                type="string", description="Filtra por retenção",
                enum=["temp", "perm"], required=False,
            ),
            "origin": ToolParameter(
                type="string", description="Filtra pela origem da ingestão",
                enum=["upload-desktop", "upload-panel", "whatsapp", "url", "workbench", "research", "job"],
                required=False,
            ),
            "q": ToolParameter(
                type="string", description="Busca por parte do nome do arquivo", required=False,
            ),
            "limit": ToolParameter(
                type="integer", description="Máximo de resultados (default 50)", required=False,
            ),
        },
    ),
    ToolSchema(
        name="file_info",
        description=(
            "Metadados de um arquivo do catálogo: tipo, tamanho, nº de linhas/páginas, "
            "preview do início e estado da extração. Chame SEMPRE antes de file_read — "
            "é ele que diz se vale ler por fatia, por página, ou se é melhor materializar "
            "com file_stage e processar no sandbox."
        ),
        module="files",
        execution="server-side",
        timeout=15,
        parameters={
            "file_id": ToolParameter(
                type="string", description="id do arquivo (vem do anexo, de file_list ou do upload)",
                required=True,
            ),
        },
    ),
    ToolSchema(
        name="file_read",
        description=(
            "Lê um PEDAÇO de um arquivo de texto do catálogo (nunca o arquivo inteiro). "
            "Informe start_line/max_lines; a resposta traz has_more e next_start_line para "
            "você continuar SÓ se precisar. Há um teto de caracteres por chamada. Para "
            "PDF/DOCX/XLSX a extração ainda não existe — use file_stage + neo_bash. Para "
            "ler rascunho do seu sandbox use neo_read_file."
        ),
        module="files",
        execution="server-side",
        timeout=20,
        parameters={
            "file_id": ToolParameter(type="string", description="id do arquivo", required=True),
            "start_line": ToolParameter(
                type="integer", description="Linha inicial, 1-based (default 1)", required=False,
            ),
            "max_lines": ToolParameter(
                type="integer", description="Quantas linhas ler (default 200)", required=False,
            ),
        },
    ),
    ToolSchema(
        name="file_stage",
        description=(
            "Materializa um arquivo do catálogo DENTRO do seu sandbox (/workspace/files/…) "
            "e devolve o path. Use para trabalho pesado (planilha grande, PDF, CSV de "
            "milhares de linhas): o arquivo REAL fica manipulável por neo_bash (pandas, "
            "wc, grep) sem nunca passar pelo seu contexto. É o caminho certo quando "
            "file_read seria caro ou o formato é binário."
        ),
        module="files",
        execution="server-side",
        timeout=90,
        parameters={
            "file_id": ToolParameter(type="string", description="id do arquivo", required=True),
            "target": ToolParameter(
                type="string", description="Ambiente de destino (hoje só 'workbench')",
                enum=["workbench"], required=False,
            ),
            "name": ToolParameter(
                type="string", description="Nome do arquivo no destino (default: o original)",
                required=False,
            ),
        },
    ),
    ToolSchema(
        name="file_save",
        description=(
            "Guarda no CATÁLOGO um arquivo que VOCÊ produziu no sandbox (/workspace), "
            "para o usuário poder baixar/consultar depois. Use quando o usuário disser "
            "'guarda isso' ou quando um job gerar um resultado que vale manter. "
            "retention='perm' não expira; 'temp' some depois do TTL. A interface do "
            "cliente já renderiza um botão de download pra este arquivo sozinha — só "
            "confirme em uma frase curta. Você NÃO tem uma URL pública: NUNCA "
            "construa/adivinhe um link de download (nem domínio, nem path)."
        ),
        module="files",
        execution="server-side",
        timeout=60,
        parameters={
            "path": ToolParameter(
                type="string",
                description="Caminho no seu sandbox (ex.: /workspace/saida/relatorio.csv)",
                required=True,
            ),
            "name": ToolParameter(
                type="string", description="Nome para o catálogo (default: o do arquivo)",
                required=False,
            ),
            "retention": ToolParameter(
                type="string", description="perm (guarda) ou temp (expira). Default perm.",
                enum=["perm", "temp"], required=False,
            ),
        },
    ),
    ToolSchema(
        name="file_offer",
        description=(
            "Mostra ao usuário o botão de download de um arquivo que JÁ está no seu catálogo "
            "(tem file_id), sem publicar de novo. Use para 'me mostra o botão de download', "
            "'me manda de novo o arquivo', 'quero baixar a versão X'. O file_id vem do registro "
            "de arquivos da conversa. Para algo que só existe no sandbox, é file_save."
        ),
        module="files",
        execution="server-side",
        timeout=15,
        parameters={
            "file_id": ToolParameter(
                type="string", description="file_id da versão a oferecer (do registro de arquivos)", required=True,
            ),
        },
    ),
    ToolSchema(
        name="file_set_retention",
        description=(
            "Muda a retenção de um arquivo do catálogo: 'perm' quando o usuário disser "
            "'guarda esse arquivo', 'temp' quando disser 'pode jogar fora depois'. "
            "Você NÃO apaga arquivos — quem apaga é o usuário pelo painel."
        ),
        module="files",
        execution="server-side",
        timeout=15,
        parameters={
            "file_id": ToolParameter(type="string", description="id do arquivo", required=True),
            "retention": ToolParameter(
                type="string", description="Nova retenção", enum=["temp", "perm"], required=True,
            ),
        },
    ),
]

FILE_TOOL_NAMES = frozenset(t.name for t in FILE_TOOLS)
