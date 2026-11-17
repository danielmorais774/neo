"""O caminho de uma transcrição (docs/transcricao-groq.md).

1. ``ffprobe`` no sandbox: tem áudio? quanto dura?
2. Com "só a cota gratuita" ligado, a conta ANTES de gastar: o vídeo inteiro
   cabe na cota agora, ou em poucos segundos de espera? Se não cabe, recusa sem
   ter mandado nada.
3. ``ffmpeg`` no sandbox tira só o áudio (``-vn``: não decodifica imagem) em opus
   16 kHz mono, cortado em pedaços (``[transcricao] parte_max_s``) que cabem no
   tamanho máximo de arquivo da cota.
4. O neo-core lê cada pedaço pela montagem SOMENTE-LEITURA do ``/workspace`` e
   manda ao Groq. O sandbox nunca vê a chave: ela fica aqui, e a rede do sandbox
   nem alcança a API (só o proxy de saída).
5. Grava ``<nome>.transcricao.txt`` e ``.srt`` ao lado do arquivo e devolve o
   texto (até ``previa_max``), dizendo quando cortou.

Sem sinal de vida o turno morre: cada etapa chama ``touch_progress()``.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import math
import os
import re
import shlex
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from core.liveness import touch_progress
from core.transcricao import config, groq
from core.transcricao.cota import Cota, cota_do_processo
from core.transcricao.texto import duracao_legivel, para_srt, para_txt

logger = logging.getLogger(__name__)

#: Texto por ``docker exec`` ao gravar o resultado. O script inteiro vai num
#: argumento só, e o Linux corta argumento acima de 128 KiB (MAX_ARG_STRLEN):
#: 60 KB de texto viram ~80 KB de base64.
_PEDACO_GRAVACAO = 60_000
_PARTE_RE = re.compile(r"^parte_\d{3}\.ogg$")
_IDIOMA_RE = re.compile(r"^[a-z]{2,3}$")
#: Pausa antes de repetir um pedido que falhou por rede ou por erro do Groq.
_PAUSA_REPETIR_S = 3.0
_CONTEXTO_MAX = 600


def _ajustes() -> Any:
    from shared.ajustes import ajustes

    return ajustes().transcricao


class Transcritor:
    """Faz a transcrição de um arquivo do ``/workspace`` (ou do catálogo)."""

    def __init__(
        self,
        *,
        sandbox: Any,
        db: Any = None,
        workspace_local: str = "/workspace",
        get_file_store: Callable[[], Any] = lambda: None,
        cota: Cota | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        dormir: Callable[[float], Awaitable[None]] = asyncio.sleep,
        ajustes: Callable[[], Any] = _ajustes,
    ) -> None:
        self.sandbox = sandbox
        self.db = db
        self.workspace_local = Path(workspace_local)
        self.get_file_store = get_file_store
        self.cota = cota or cota_do_processo(db)
        self.transport = transport
        self._dormir = dormir
        self._ajustes = ajustes

    # ------------------------------------------------------------------
    # Entrada
    # ------------------------------------------------------------------

    async def transcrever(
        self,
        *,
        caminho: str | None = None,
        file_id: str | None = None,
        idioma: str | None = None,
        contexto: str | None = None,
    ) -> dict[str, Any]:
        chave = config.chave()
        if not chave:
            return _falha(
                "não há chave do Groq configurada. Grave uma em neo-panel → Configurações → Transcrição "
                "(ou GROQ_API_KEY no .env)."
            )
        aj = self._ajustes()

        idioma_final, erro = _idioma(idioma, aj.idioma_padrao)
        if erro:
            return _falha(erro)
        prompt = " ".join(str(contexto or "").split())[:_CONTEXTO_MAX] or None

        origem, erro = await self._origem(caminho, file_id)
        if erro:
            return _falha(erro)

        touch_progress()
        sonda, erro = await self._sondar(origem, aj)
        if erro:
            return _falha(erro)
        duracao = sonda

        so_gratuita = config.so_gratuita()
        limites = aj.cota
        parte_s = _duracao_da_parte(aj.parte_max_s, aj.bitrate_kbps, limites.arquivo_max_mb)
        n_partes = max(1, math.ceil(duracao / parte_s))
        if duracao > aj.duracao_max_s:
            return _falha(
                f"o áudio tem {duracao_legivel(duracao)}, acima do máximo de "
                f"{duracao_legivel(aj.duracao_max_s)} ([transcricao] duracao_max_s no neo.toml). "
                "Corte o arquivo em partes menores com ffmpeg e transcreva cada uma."
            )
        if so_gratuita:
            espera, balde = await self.cota.espera(duracao, pedidos=n_partes)
            if espera == math.inf:
                cap = self.cota.capacidades()[balde][0]
                return _falha(
                    f"o áudio tem {duracao_legivel(duracao)} e não cabe na cota gratuita "
                    f"({_nome_do_balde(balde)}: {_quantidade(balde, cap)}). Corte o arquivo em partes menores.",
                    cota=await self._cota_resumo(),
                )
            if espera > aj.espera_max_s:
                return _falha(
                    f"a cota gratuita do Groq está ocupada ({_nome_do_balde(balde)}). Dá para transcrever "
                    f"este áudio ({duracao_legivel(duracao)}) daqui a ~{max(1, math.ceil(espera / 60))} min.",
                    cota=await self._cota_resumo(),
                    volta_em_s=int(math.ceil(espera)),
                )

        # Depois da conta da cota (que é de graça) e antes de extrair: mede o som.
        touch_progress()
        com_som = await self._fracao_com_som(origem, duracao, aj)
        if com_som is not None and com_som < aj.som_min_fracao:
            return _falha(
                f"o áudio de {origem} é quase todo silêncio ({100 * com_som:.0f}% com som): não há fala "
                "para transcrever. Se o que importa é o TEXTO que aparece na tela (gravação de "
                "conversa, slides), o caminho é a skill ocr-video.",
                com_som=round(com_som, 3),
            )

        trabalho = f"{self.sandbox.workspace_root}/.neo/transcricao/{uuid.uuid4().hex[:12]}"
        t0 = time.monotonic()
        try:
            partes, erro = await self._extrair(origem, trabalho, parte_s, aj)
            if erro:
                return _falha(erro)
            resultado = await self._transcrever_partes(
                partes, chave=chave, idioma=idioma_final, prompt=prompt, so_gratuita=so_gratuita, aj=aj
            )
            if not resultado["segmentos"]:
                motivo = resultado["parou"] or (
                    f"o Whisper não achou fala: os {resultado['sem_fala']} trecho(s) que ele devolveu "
                    "eram não-fala (música, ruído, silêncio). Se o que importa é o texto na tela, "
                    "o caminho é a skill ocr-video."
                    if resultado["sem_fala"]
                    else "o Groq não devolveu fala nenhuma (áudio sem voz?)."
                )
                return _falha(motivo, cota=await self._cota_resumo())
            return await self._entregar(origem, duracao, resultado, trabalho, aj, time.monotonic() - t0)
        finally:
            await self._limpar(trabalho)

    async def _origem(self, caminho: str | None, file_id: str | None) -> tuple[str, str]:
        """O caminho do arquivo DENTRO do sandbox, ou a mensagem de erro."""
        if str(file_id or "").strip():
            store = self.get_file_store()
            if store is None:
                return "", "o catálogo de arquivos está desligado: passe o caminho no /workspace."
            try:
                ref = await store.stage(str(file_id).strip(), target="workbench")
            except Exception as e:  # noqa: BLE001 — FileStoreError e companhia
                return "", f"não consegui trazer o arquivo {file_id} para o sandbox: {getattr(e, 'message', None) or e}"
            destino = str(ref.get("path") or "")
            if not destino:
                return "", f"o catálogo não disse onde pôs o arquivo {file_id}."
            return destino, ""
        if not str(caminho or "").strip():
            return "", "passe 'caminho' (no /workspace) ou 'file_id' (do catálogo)."
        try:
            return self.sandbox.resolve_path(str(caminho).strip()), ""
        except ValueError as e:
            return "", (
                f"{e}. Esta tool só lê o SEU sandbox. Arquivo do computador do usuário: traga com "
                "request_user_file primeiro."
            )

    async def _sondar(self, origem: str, aj: Any) -> tuple[float, str]:
        comando = (
            "ffprobe -v error -show_entries format=duration:stream=codec_type -of json "
            f"{shlex.quote(origem)}"
        )
        r = await self.sandbox.run(comando, timeout=aj.extracao_timeout_s)
        if not r.ok:
            detalhe = (r.stderr or r.stdout or "").strip()[-300:]
            if "No such file" in detalhe:
                return 0.0, f"não existe {origem} no sandbox."
            return 0.0, f"o ffprobe não conseguiu ler {origem}: {detalhe or f'saída {r.exit_code}'}"
        try:
            info = json.loads(r.stdout or "{}")
        except json.JSONDecodeError:
            return 0.0, f"o ffprobe devolveu algo ilegível para {origem}."
        tipos = {str(s.get("codec_type")) for s in info.get("streams") or [] if isinstance(s, dict)}
        if "audio" not in tipos:
            return 0.0, f"{origem} não tem faixa de áudio: não há o que transcrever."
        try:
            duracao = float((info.get("format") or {}).get("duration") or 0)
        except (TypeError, ValueError):
            duracao = 0.0
        if duracao <= 0:
            return 0.0, f"não consegui medir a duração de {origem}."
        return duracao, ""

    async def _fracao_com_som(self, origem: str, duracao: float, aj: Any) -> float | None:
        """Quanto do áudio tem som, pelo ``silencedetect`` do ffmpeg (só decodifica o áudio).

        ``None`` quando a medição não saiu: aí a transcrição segue (medir é
        proteção, não requisito). O ``awk`` soma dentro do sandbox, porque um vídeo
        longo dá milhares de linhas e a saída do ``run`` é cortada em 60 KB. Um
        ``silence_start`` sem ``silence_end`` é silêncio até o fim do arquivo.
        """
        filtro = f"silencedetect=noise={float(aj.silencio_db):g}dB:d={float(aj.silencio_min_s):g}"
        soma = (
            "awk '/silence_duration/ {s+=$NF} /silence_start/ {u=$NF; a=1} /silence_end/ {a=0} "
            "END {printf \"%.3f %d %.3f\\n\", s, a, u}'"
        )
        comando = (
            f"ffmpeg -nostdin -hide_banner -i {shlex.quote(origem)} -map 0:a:0 -vn -ac 1 -ar 16000 "
            f"-af {filtro} -f null - 2>&1 | {soma}"
        )
        try:
            r = await self.sandbox.run(comando, timeout=aj.extracao_timeout_s, stall_timeout=120)
        except Exception as e:  # noqa: BLE001
            logger.warning("[transcricao] medição de som falhou: %s", e)
            return None
        touch_progress()
        try:
            silencio, aberto, inicio = (r.stdout or "").split()[:3]
            total = float(silencio) + ((duracao - float(inicio)) if int(aberto) else 0.0)
        except (ValueError, TypeError):
            return None
        return max(0.0, min(1.0, 1.0 - total / duracao)) if duracao > 0 else None

    # ------------------------------------------------------------------
    # Áudio
    # ------------------------------------------------------------------

    async def _extrair(
        self, origem: str, trabalho: str, parte_s: int, aj: Any
    ) -> tuple[list[tuple[Path, float, float]], str]:
        lista = f"{trabalho}/partes.csv"
        comando = (
            f"mkdir -p {shlex.quote(trabalho)} && "
            f"ffmpeg -nostdin -hide_banner -loglevel error -y -i {shlex.quote(origem)} "
            f"-vn -map 0:a:0 -ac 1 -ar 16000 -c:a libopus -b:a {int(aj.bitrate_kbps)}k "
            f"-f segment -segment_format ogg -segment_time {int(parte_s)} -reset_timestamps 1 "
            f"-segment_list {shlex.quote(lista)} -segment_list_type csv "
            f"{shlex.quote(trabalho + '/parte_%03d.ogg')}"
        )
        touch_progress()
        r = await self.sandbox.run(comando, timeout=aj.extracao_timeout_s, stall_timeout=120)
        touch_progress()
        if not r.ok:
            detalhe = (r.stderr or r.stdout or "").strip()[-300:]
            if getattr(r, "timed_out", False):
                return [], f"tirar o áudio passou de {aj.extracao_timeout_s}s e foi interrompido."
            return [], f"o ffmpeg não conseguiu tirar o áudio: {detalhe or f'saída {r.exit_code}'}"
        try:
            csv_local = self._local(lista)
            linhas = list(csv.reader(io.StringIO(csv_local.read_text(encoding="utf-8"))))
        except (OSError, ValueError) as e:
            return [], f"a lista de pedaços do áudio não ficou legível: {e}"
        partes: list[tuple[Path, float, float]] = []
        for linha in linhas:
            if len(linha) < 3:
                continue
            nome = linha[0].strip().rsplit("/", 1)[-1]
            if not _PARTE_RE.match(nome):
                continue
            try:
                inicio, fim = float(linha[1]), float(linha[2])
                local = self._local(f"{trabalho}/{nome}")
            except (ValueError, OSError):
                continue
            if local.is_file():
                partes.append((local, inicio, max(inicio, fim)))
        if not partes:
            return [], "o ffmpeg terminou sem gerar áudio."
        return partes, ""

    def _local(self, caminho_no_sandbox: str) -> Path:
        """O mesmo arquivo pela montagem do core, sem seguir symlink para fora."""
        raiz = self.sandbox.workspace_root.rstrip("/")
        if caminho_no_sandbox != raiz and not caminho_no_sandbox.startswith(raiz + "/"):
            raise ValueError(f"fora do workspace: {caminho_no_sandbox}")
        relativo = caminho_no_sandbox[len(raiz):].lstrip("/")
        alvo = self.workspace_local / relativo
        real = Path(os.path.realpath(alvo))
        raiz_real = Path(os.path.realpath(self.workspace_local))
        if real != raiz_real and raiz_real not in real.parents:
            raise ValueError(f"o caminho sai do workspace (symlink?): {caminho_no_sandbox}")
        return real

    # ------------------------------------------------------------------
    # Groq
    # ------------------------------------------------------------------

    async def _transcrever_partes(
        self,
        partes: list[tuple[Path, float, float]],
        *,
        chave: str,
        idioma: str | None,
        prompt: str | None,
        so_gratuita: bool,
        aj: Any,
    ) -> dict[str, Any]:
        modelo = config.modelo()
        segmentos: list[dict[str, Any]] = []
        idiomas: list[str] = []
        parou = ""
        feitas = 0
        sem_fala = 0
        limite_mb = aj.cota.arquivo_max_mb
        for indice, (arquivo, inicio, fim) in enumerate(partes):
            custo = max(1.0, fim - inicio)
            tamanho = arquivo.stat().st_size
            if so_gratuita and tamanho > limite_mb * 1_000_000:
                parou = (
                    f"o pedaço {indice + 1} tem {tamanho / 1_000_000:.1f} MB, acima dos {limite_mb} MB da cota "
                    "gratuita. Baixe [transcricao] bitrate_kbps ou parte_max_s no neo.toml."
                )
                break
            if so_gratuita:
                espera, balde = await self.cota.espera(custo, 1)
                if espera == math.inf or espera > aj.espera_max_s:
                    volta = "" if espera == math.inf else f" Volta a ter espaço em ~{max(1, math.ceil(espera / 60))} min."
                    parou = f"parei no pedaço {indice + 1} de {len(partes)}: a cota gratuita acabou ({_nome_do_balde(balde)}).{volta}"
                    break
                if espera > 0:
                    await self._esperar(espera)
            dados = arquivo.read_bytes()
            resposta, erro = await self._pedir(
                dados, nome=arquivo.name, chave=chave, modelo=modelo, idioma=idioma, prompt=prompt,
                custo=custo, so_gratuita=so_gratuita, aj=aj,
            )
            touch_progress()
            if erro:
                parou = f"parei no pedaço {indice + 1} de {len(partes)}: {erro}" if len(partes) > 1 else erro
                break
            assert resposta is not None
            feitas += 1
            if resposta.dados.get("language"):
                idiomas.append(str(resposta.dados["language"]))
            mantidos, fora = _segmentos(
                resposta.dados, deslocamento=inicio, duracao=fim - inicio,
                prob_max=float(aj.sem_fala_prob), logprob_min=float(aj.sem_fala_logprob),
            )
            segmentos.extend(mantidos)
            sem_fala += fora
        return {
            "segmentos": segmentos,
            "parou": parou,
            "feitas": feitas,
            "sem_fala": sem_fala,
            "total": len(partes),
            "modelo": modelo,
            "idioma": idiomas[0] if idiomas else (idioma or ""),
        }

    async def _pedir(
        self,
        dados: bytes,
        *,
        nome: str,
        chave: str,
        modelo: str,
        idioma: str | None,
        prompt: str | None,
        custo: float,
        so_gratuita: bool,
        aj: Any,
    ) -> tuple[groq.Resposta | None, str]:
        """Um pedaço, com no máximo uma repetição. A cota é anotada antes de mandar."""
        for tentativa in range(2):
            await self.cota.gastar(custo, 1)
            try:
                resposta = await groq.transcrever(
                    chave, dados=dados, nome=nome, modelo=modelo, idioma=idioma, prompt=prompt,
                    timeout=float(aj.pedido_timeout_s), transport=self.transport,
                )
            except groq.GroqErro as e:
                if e.cabecalhos:
                    await self.cota.sincronizar(e.cabecalhos)
                # 5xx: o Groq pode ter contado o pedido, e a conta fica como está
                # (lado conservador). O resto foi recusado antes de transcrever.
                if e.tipo != "servidor":
                    await self.cota.devolver(custo, 1)
                ultima = tentativa == 1
                if e.tipo == "limite":
                    espera = e.retry_after if e.retry_after is not None else 10.0
                    if ultima or espera > aj.espera_max_s:
                        return None, f"o Groq pediu para esperar {math.ceil(espera)}s (limite da conta)."
                    await self._esperar(espera)
                    continue
                if e.tipo in ("servidor", "rede") and not ultima:
                    await self._esperar(_PAUSA_REPETIR_S)
                    continue
                if e.tipo == "modelo":
                    return None, f"{e.mensagem}. Escolha outro modelo em neo-panel → Configurações → Transcrição."
                if e.tipo == "chave":
                    return None, f"{e.mensagem}. Confira a chave em neo-panel → Configurações → Transcrição."
                return None, e.mensagem
            await self.cota.sincronizar(resposta.cabecalhos)
            return resposta, ""
        return None, "o Groq não respondeu."

    async def _esperar(self, segundos: float) -> None:
        """Espera a cota encher sem o turno parecer travado."""
        resta = max(0.0, float(segundos))
        while resta > 0:
            touch_progress()
            passo = min(5.0, resta)
            await self._dormir(passo)
            resta -= passo
        touch_progress()

    # ------------------------------------------------------------------
    # Saída
    # ------------------------------------------------------------------

    async def _entregar(
        self, origem: str, duracao: float, resultado: dict[str, Any], trabalho: str, aj: Any, gasto_s: float
    ) -> dict[str, Any]:
        base = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", origem)
        txt_path, srt_path = f"{base}.transcricao.txt", f"{base}.transcricao.srt"
        texto = para_txt(resultado["segmentos"])
        erro = await self._gravar(txt_path, texto, trabalho) or await self._gravar(
            srt_path, para_srt(resultado["segmentos"]), trabalho
        )
        if erro:
            return _falha(f"transcrevi, mas não consegui gravar o resultado: {erro}")
        previa = texto[: aj.previa_max]
        cortado = len(texto) > aj.previa_max
        parcial = bool(resultado["parou"])
        logger.info(
            "[transcricao] %s: %s de áudio, %d/%d pedaço(s), modelo %s, %.1fs%s",
            origem, duracao_legivel(duracao), resultado["feitas"], resultado["total"], resultado["modelo"],
            gasto_s, " (PARCIAL)" if parcial else "",
        )
        saida: dict[str, Any] = {
            "ok": True,
            "arquivo_txt": txt_path,
            "arquivo_srt": srt_path,
            "duracao": duracao_legivel(duracao),
            "idioma": resultado["idioma"],
            "modelo": resultado["modelo"],
            "pedacos": f"{resultado['feitas']} de {resultado['total']}",
            "caracteres": len(texto),
            "trechos_sem_fala_descartados": resultado["sem_fala"],
            "texto": previa,
            "texto_cortado": cortado,
            "cota": await self._cota_resumo(),
        }
        if parcial:
            saida["parcial"] = True
            saida["aviso"] = (
                f"Transcrição INCOMPLETA: {resultado['parou']} O que saiu está nos arquivos. "
                "Diga isso ao usuário."
            )
        notas = []
        if cortado:
            notas.append(
                f"O texto acima é só o começo ({aj.previa_max} de {len(texto)} caracteres). O inteiro está em "
                f"{txt_path}: leia com neo_read_file, ou use deep_read se for longo."
            )
        notas.append(
            f"A legenda com os tempos está em {srt_path}. Para o usuário baixar, entregue com file_save."
        )
        saida["nota"] = " ".join(notas)
        return saida

    async def _gravar(self, destino: str, conteudo: str, trabalho: str) -> str:
        """Grava pelo sandbox (o core só lê o /workspace). Devolve o erro, ou ``""``."""
        pedacos = _fatiar(conteudo, _PEDACO_GRAVACAO)
        if len(pedacos) <= 1:
            r = await self.sandbox.write_file(destino, conteudo)
            return "" if r.ok else (r.stderr or f"saída {r.exit_code}").strip()[:300]
        nomes = []
        for k, pedaco in enumerate(pedacos):
            nome = f"{trabalho}/saida_{k:03d}"
            r = await self.sandbox.write_file(nome, pedaco)
            if not r.ok:
                return (r.stderr or f"saída {r.exit_code}").strip()[:300]
            nomes.append(shlex.quote(nome))
        r = await self.sandbox.run(f"cat {' '.join(nomes)} > {shlex.quote(destino)}", timeout=60)
        return "" if r.ok else (r.stderr or f"saída {r.exit_code}").strip()[:300]

    async def _limpar(self, trabalho: str) -> None:
        try:
            await self.sandbox.run(f"rm -rf {shlex.quote(trabalho)}", timeout=60)
        except Exception as e:  # noqa: BLE001 — a faxina do /workspace pega o resto
            logger.warning("[transcricao] não apaguei %s: %s", trabalho, e)

    async def _cota_resumo(self) -> dict[str, Any]:
        try:
            estado = await self.cota.estado()
        except Exception:  # noqa: BLE001
            return {}
        return {
            "audio_nesta_hora": duracao_legivel(estado["audio_hora"]["restante"]),
            "audio_hoje": duracao_legivel(estado["audio_dia"]["restante"]),
            "pedidos_hoje": int(estado["pedidos_dia"]["restante"]),
            "so_cota_gratuita": config.so_gratuita(),
        }


# ----------------------------------------------------------------------
# Funções puras
# ----------------------------------------------------------------------


def _falha(mensagem: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": mensagem, **{k: v for k, v in extra.items() if v not in (None, {}, "")}}


def _idioma(pedido: str | None, padrao: str) -> tuple[str | None, str]:
    bruto = str(pedido or "").strip().lower() or str(padrao or "").strip().lower()
    if not bruto or bruto == "auto":
        return None, ""
    codigo = re.split(r"[-_]", bruto)[0]
    if not _IDIOMA_RE.match(codigo):
        return None, f"idioma {pedido!r} não é um código ISO-639-1 (ex.: pt, en, es) nem 'auto'."
    return codigo, ""


def _duracao_da_parte(parte_max_s: int, bitrate_kbps: int, arquivo_max_mb: int) -> int:
    """Pedaço que cabe no arquivo máximo, com 10% de folga e 5% de contêiner."""
    bytes_por_s = bitrate_kbps * 1000 / 8 * 1.05
    cabe = int(arquivo_max_mb * 1_000_000 * 0.9 / bytes_por_s)
    return max(30, min(int(parte_max_s), cabe))


def _sem_fala(s: dict[str, Any], prob_max: float, logprob_min: float) -> bool:
    """A regra de silêncio da implementação de referência do Whisper.

    O trecho é não-fala quando ``no_speech_prob`` passa do limiar E a confiança
    média (``avg_logprob``) fica abaixo da outra: um trecho com fala clara pode ter
    ``no_speech_prob`` alto e mesmo assim ser texto certo. Sem os dois campos, fica.
    """
    try:
        prob = float(s["no_speech_prob"])
        logprob = float(s["avg_logprob"])
    except (KeyError, TypeError, ValueError):
        return False
    return prob > prob_max and logprob < logprob_min


def _segmentos(
    dados: dict[str, Any],
    *,
    deslocamento: float,
    duracao: float,
    prob_max: float = 0.6,
    logprob_min: float = -1.0,
) -> tuple[list[dict[str, Any]], int]:
    """Os trechos com o tempo do arquivo inteiro, e quantos saíram por não-fala."""
    brutos = dados.get("segments")
    saida: list[dict[str, Any]] = []
    descartados = 0
    if isinstance(brutos, list) and brutos:
        for s in brutos:
            if not isinstance(s, dict):
                continue
            if _sem_fala(s, prob_max, logprob_min):
                descartados += 1
                continue
            try:
                inicio = float(s.get("start") or 0.0) + deslocamento
                fim = float(s.get("end") or 0.0) + deslocamento
            except (TypeError, ValueError):
                continue
            saida.append({"start": inicio, "end": max(inicio, fim), "text": str(s.get("text") or "")})
        return saida, descartados
    texto = str(dados.get("text") or "").strip()
    return ([{"start": deslocamento, "end": deslocamento + duracao, "text": texto}] if texto else []), 0


def _fatiar(texto: str, max_bytes: int) -> list[str]:
    """Pedaços de até ``max_bytes`` em UTF-8, cortando em fim de linha quando dá."""
    if len(texto.encode("utf-8")) <= max_bytes:
        return [texto]
    pedacos: list[str] = []
    atual: list[str] = []
    tamanho = 0
    for linha in texto.splitlines(keepends=True):
        while len(linha.encode("utf-8")) > max_bytes:
            corte = max_bytes // 4  # caracteres: até 4 bytes cada em UTF-8
            if atual:
                pedacos.append("".join(atual))
                atual, tamanho = [], 0
            pedacos.append(linha[:corte])
            linha = linha[corte:]
        n = len(linha.encode("utf-8"))
        if tamanho + n > max_bytes and atual:
            pedacos.append("".join(atual))
            atual, tamanho = [], 0
        atual.append(linha)
        tamanho += n
    if atual:
        pedacos.append("".join(atual))
    return pedacos


def _nome_do_balde(balde: str) -> str:
    return {
        "audio_hora": "áudio por hora",
        "audio_dia": "áudio por dia",
        "pedidos_minuto": "pedidos por minuto",
        "pedidos_dia": "pedidos por dia",
    }.get(balde, balde)


def _quantidade(balde: str, valor: float) -> str:
    return duracao_legivel(valor) if balde.startswith("audio") else f"{int(valor)} pedidos"
