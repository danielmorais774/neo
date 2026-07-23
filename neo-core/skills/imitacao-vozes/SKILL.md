---
name: imitacao-vozes
description: >
  Imitar vozes/personagens a pedido ("imita o X", "faz a voz do Y", "fala como
  o Z"): confere quais vozes existem e monta a fala com a voz certa marcada.
keywords: [imita, imite, imitar, imitação, imitacao, faz a voz, faça a voz, faz uma voz, fala como, fala igual, dubla, dublar, personagem, imitando]
kind: tool_backed
register: oficina
tools: [voice_list]
enabled: true
version: 1
---

# Imitação de vozes

## Quando usar
- "imita o Mario", "faz a voz do Jarvis", "fala como o Silvio Santos", "dubla o
  Homer pra mim".
- NÃO use para *trocar* a voz padrão do Neo de forma permanente — isso é
  configuração (`config_set`, setting `tts.voice`). Imitação é pontual: só nesta
  resposta.

## Como fazer (nesta ordem)

1. **Veja o que existe.** Chame `voice_list` com `engine="coqui"` (por enquanto a
   imitação só usa o coqui). O retorno lista as vozes embutidas e os **clones**
   (cada clone tem um nome amigável, ex.: "mario", "jarvis"). É esse nome que você
   usa pra marcar a fala.

2. **Se existe a voz pedida → monte a resposta com a marcação.** Envolva **apenas
   as falas do personagem** em `[[VOICE:nome]]...[[/VOICE]]`. O resto (sua
   narração, o "ok, vou imitar") fica **sem tag** → sai na sua voz padrão.

   Exemplo — pediram pra imitar o Mario e existe o clone "mario":
   ```
   Ok, vou imitar o Mario pra você. [[VOICE:mario]]Oi! Eu sou o Mario! Vamos nessa![[/VOICE]]
   ```
   Isso vira duas falas: a primeira na sua voz, a segunda na voz do Mario.

   Dá pra misturar mais de uma voz na mesma resposta — é só abrir outra tag:
   ```
   Deixa eu fazer os dois. [[VOICE:mario]]É uma me!![[/VOICE]] E agora o mordomo: [[VOICE:jarvis]]Às suas ordens, senhor.[[/VOICE]]
   ```

3. **Se NÃO existe a voz pedida → seja honesto.** Diga que não tem a voz daquela
   pessoa/personagem (não invente, não use uma tag com nome que não apareceu no
   `voice_list`). Se fizer sentido, ofereça as que você tem: "não tenho a voz do
   X, mas tenho a do Mario e a do Jarvis — quer uma dessas?".

## Regras da marcação (importante)
- O nome dentro de `[[VOICE:nome]]` tem que ser um nome que **apareceu no
  `voice_list`** (o nome amigável do clone). Nome desconhecido é ignorado e a fala
  sai na voz padrão — ou seja, não adianta inventar.
- A tag envolve **só o texto que deve ser falado naquela voz**. Não coloque a
  narração dentro da tag.
- Não aninhe tags. Feche cada `[[VOICE:...]]` com `[[/VOICE]]` antes de abrir
  outra.
- Escreva as falas do personagem de um jeito natural pra TTS (sem markdown, sem
  emoji dentro da tag) — vira áudio.

## Fora de escopo
- **Criar/clonar** uma voz nova: isso é feito no neo-voice (`/clone`), fora daqui.
  Esta skill só **usa** clones que já existem. Se o usuário quer que você "aprenda"
  a voz de alguém, explique que precisa cadastrar o clone antes.
