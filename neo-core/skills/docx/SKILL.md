---
name: docx
description: >
  Edita e preenche documento Word (.docx) sem estragar o resto: campos de template
  (nome, R.A., prazo), inserir ou formatar texto, trocar trecho — e confere e olha a
  página antes de entregar.
keywords: [docx, word, template, cabecalho, preencher, campo, mapa, atividade, documento word, trabalho da faculdade, .docx]
kind: scripted
register: oficina
enabled: true
version: 1
# Depende da IMAGEM do workbench: python-docx (Office) e, para olhar a página,
# LibreOffice + poppler. Receita e verificação: docker/neo-workbench/DEPENDENCIAS.md.
---

# Editar .docx sem estragar

## Quando usar
- Ele mandou um .docx (template, formulário, trabalho) e pediu para preencher, pôr
  nome/R.A./data, inserir ou formatar texto, e devolver o arquivo.

## Como fazer
1. **Ponto de partida.** No registro de arquivos: o anexo desta mensagem, ou a VERSÃO
   ATUAL do documento desta conversa. Materialize com
   `file_stage(file_id=<id>, name="original.docx")` — nome neutro; não batize pelo
   assunto antes de ler.
2. **Mapa, uma chamada:** `python3 /skills/docx/scripts/docx_mapa.py /workspace/files/original.docx`.
   Leia `campos`, `imagens` e `regras_do_template`.
3. **"Cabeçalho" num template = os CAMPOS de identificação** (a tabela do topo). O
   cabeçalho de PÁGINA do Word só se ele disser "em todas as páginas".
4. **Valores só dele.** Nome, R.A., datas: o que ele disse ou a ficha dele no PKS.
   Faltou um, pergunte — não complete nome por dedução.
5. **Um arquivo de operações por documento:** `/workspace/files/<doc>.ops.json`
   (com `neo_write_file`). Cada pedido novo ACRESCENTA operações; nunca tire as
   anteriores. É a lista do que ele já pediu — é ela que impede o R.A. de sumir na
   terceira versão.
6. **Gere sempre do ORIGINAL com o arquivo inteiro:**
   `python3 /skills/docx/scripts/docx_preencher.py /workspace/files/original.docx /workspace/files/<saida>.docx --ops /workspace/files/<doc>.ops.json`.
   Operações (formato no topo do script): `campo` (o `rotulo` exatamente como o mapa
   mostra), `substituir`, `inserir`.
7. **Conteúdo longo entra por `"de_arquivo"`** (uma linha por parágrafo, `# ` negrito,
   `> ` recuo). Monte o .txt POR CÓDIGO a partir da fonte — nunca redigite: perde
   acento e inventa. Conteúdo de outro arquivo só se ele pediu; se o registro disser
   que é de OUTRA conversa, confirme com ele antes.
8. **Formatação:** aplique ao que inserir as `regras_do_template` (ex.: Arial/Times 12,
   espaçamento 1,5, justificado) via `"formato"`. Título, instruções e logo do
   template ficam — não apague o que ele não pediu.
9. **`ok: false` não se entrega.** O `.REPROVADO.docx` nunca vai para `file_save`:
   leia `nao_aplicadas` e `conferencia.problemas`, corrija e rode de novo.
10. **Olhe a página, comparando:** renderize o original E a saída
    (`python3 /skills/docx/scripts/docx_renderizar.py <arquivo>.docx --paginas 1`, um de
    cada vez) e chame `file_olhar(imagens=[<página do original>, <página da saída>],
    pergunta="a 1ª imagem é o original e a 2ª a versão nova: o que mudou além dos campos X, Y, Z? sumiu logo ou texto? com que valores os campos ficaram?")`.
    Olhando só a página nova, a visão confunde o desenho do template com defeito.
    Exit 3 = sem LibreOffice: siga, e diga a ele que não viu a página.
11. **Entregue:** `file_save(path=<saida>, name=<o nome que o template pede ou que ele
    disse>, versao_de=<caminho ou file_id da versão de partida>)`.
12. **Responda só o que a conferência e o file_olhar confirmaram**, com os valores como
    estão no arquivo.

## Regras
- python-docx direto: NUNCA `p.text = ...` em parágrafo com imagem (é o logo) e nunca
  remova parágrafos do template.
- Prefira os scripts a código avulso. Fez script próprio? Passe a saída pelo
  `docx_conferir.py ORIGINAL SAIDA --espera <cada valor dele>` antes de salvar.
