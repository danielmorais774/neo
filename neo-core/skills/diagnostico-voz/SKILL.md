---
name: diagnostico-voz
description: >
  Diagnóstico passo a passo quando a voz do Neo falha ou soa errada (STT não
  transcreve, TTS mudo, voz/engine trocada). Use quando o usuário reclamar da
  fala ou da escuta do Neo.
keywords: [voz, tts, stt, fala, falar, escutar, microfone, engine, mudo, transcricao]
kind: instructional
enabled: true
version: 1
---

# Diagnóstico de voz (STT/TTS)

## Quando usar
- "o Neo não está falando", "a voz sumiu", "não me escuta", "a voz está estranha/trocada".

## Como fazer (nesta ordem — pare quando achar a causa)

1. **Qual voz/engine está ativa?** Chame `voice_get`. Se a voz ativa não é a que
   o usuário espera, é troca de configuração — confirme com ele antes de sugerir
   mudar (a mudança em si é `config_set`, setting `tts.voice`/`tts.engine`, que
   roda fora deste turno).
2. **A voz pedida existe?** Chame `voice_list` (filtre por engine se souber).
   Voz inexistente/clone removido explica "voz trocada".
3. **Config coerente?** Chame `config_get` para `tts.engine` e `tts.voice` e
   compare com o passo 1. Divergência = preferência corrompida; explique e
   proponha regravar a escolha.
4. **Nada disso?** Então a causa provável é infra (engine caída no PC gamer,
   portproxy do WSL2 perdido após reboot, engine monitor em fallback). Você NÃO
   tem tool para consertar isso daqui — explique ao usuário o que provavelmente
   é e oriente: abrir o painel de engines nas Settings do app desktop (sobe/baixa
   engines por SSH e mostra o status de /health de cada uma).

## Regras
- Reporte o que CADA tool retornou de fato — nunca invente status de engine.
- Não afirme que "consertou" nada: este fluxo só diagnostica e orienta.
