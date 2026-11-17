"""Falsos compartilhados: banco de preferências, sandbox com ffmpeg de mentira e Groq."""

from __future__ import annotations

import json
import re
import shlex
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet

from core.transcricao import config
from core.workbench.sandbox import SandboxResult


class FakeDB:
    """``user_preference`` em memória: (service, category, key) -> valor."""

    def __init__(self) -> None:
        self.prefs: dict[tuple[str, str, str], str] = {}

    async def get_preferences(self, user_id=None, service="neo-desktop", category=None):
        return [
            SimpleNamespace(key=k, value=v, category=c)
            for (s, c, k), v in self.prefs.items()
            if s == service and (category is None or c == category)
        ]

    async def upsert_preference(self, category, key, value, user_id=None, service="neo-desktop", source_message=None, metadata=None):
        self.prefs[(service, category, key)] = value
        return SimpleNamespace(key=key, value=value)

    async def delete_preference(self, category, key, user_id=None, service="neo-desktop"):
        return self.prefs.pop((service, category, key), None) is not None


class FakeSandbox:
    """Sandbox cujo /workspace é uma pasta temporária.

    Entende os quatro comandos que a transcrição roda: ``ffprobe`` (devolve
    ``self.sonda``), ``ffmpeg`` (escreve os pedaços de ``self.partes`` e o CSV),
    ``cat … > destino`` e ``rm -rf``.
    """

    workspace_root = "/workspace"

    def __init__(self, raiz: Path, *, duracao: float = 90.0, partes: list[tuple[float, float]] | None = None) -> None:
        self.raiz = raiz
        self.sonda: dict[str, Any] = {
            "streams": [{"codec_type": "video"}, {"codec_type": "audio"}],
            "format": {"duration": str(duracao)},
        }
        self.partes = partes if partes is not None else [(0.0, duracao)]
        self.bytes_por_parte = 1000
        self.comandos: list[str] = []
        self.ffmpeg_falha = ""
        #: O que o ``awk`` do silencedetect imprime: "<silêncio somado> <aberto> <início>".
        self.som = "0.000 0 0.000"

    def local(self, caminho: str) -> Path:
        assert caminho.startswith("/workspace"), caminho
        return self.raiz / caminho[len("/workspace"):].lstrip("/")

    def resolve_path(self, path: str) -> str:
        raw = path.strip()
        cand = raw if raw.startswith("/") else f"/workspace/{raw}"
        if cand != "/workspace" and not cand.startswith("/workspace/"):
            raise ValueError(f"Caminho fora do workspace do Neo (/workspace): {path!r}")
        return cand

    async def run(self, command: str, *, cwd=None, timeout=None, env=None, stall_timeout=None) -> SandboxResult:
        self.comandos.append(command)
        if command.startswith("ffprobe"):
            alvo = shlex.split(command)[-1]
            if not self.local(alvo).exists():
                return SandboxResult(exit_code=1, stdout="", stderr=f"{alvo}: No such file or directory")
            return SandboxResult(exit_code=0, stdout=json.dumps(self.sonda), stderr="")
        if "silencedetect" in command:
            return SandboxResult(exit_code=0, stdout=self.som + "\n", stderr="")
        if "ffmpeg" in command:
            if self.ffmpeg_falha:
                return SandboxResult(exit_code=1, stdout="", stderr=self.ffmpeg_falha)
            pasta = re.search(r"mkdir -p (\S+)", command).group(1)
            pasta_local = self.local(shlex.split(pasta)[0])
            pasta_local.mkdir(parents=True, exist_ok=True)
            linhas = []
            for i, (ini, fim) in enumerate(self.partes):
                nome = f"parte_{i:03d}.ogg"
                (pasta_local / nome).write_bytes(bytes([i % 256]) * self.bytes_por_parte)
                linhas.append(f"{nome},{ini:.6f},{fim:.6f}")
            (pasta_local / "partes.csv").write_text("\n".join(linhas) + "\n")
            return SandboxResult(exit_code=0, stdout="", stderr="")
        if command.startswith("cat "):
            antes, _, destino = command.partition(" > ")
            fontes = shlex.split(antes)[1:]
            conteudo = "".join(self.local(f).read_text(encoding="utf-8") for f in fontes)
            alvo = self.local(shlex.split(destino)[0])
            alvo.parent.mkdir(parents=True, exist_ok=True)
            alvo.write_text(conteudo, encoding="utf-8")
            return SandboxResult(exit_code=0, stdout="", stderr="")
        if command.startswith("rm -rf "):
            shutil.rmtree(self.local(shlex.split(command)[-1]), ignore_errors=True)
            return SandboxResult(exit_code=0, stdout="", stderr="")
        return SandboxResult(exit_code=127, stdout="", stderr=f"comando inesperado: {command}")

    async def write_file(self, path: str, content: str) -> SandboxResult:
        alvo = self.local(path)
        alvo.parent.mkdir(parents=True, exist_ok=True)
        alvo.write_text(content, encoding="utf-8")
        return SandboxResult(exit_code=0, stdout="", stderr="")


class FakeGroq:
    """Transport do httpx que faz o papel da API do Groq."""

    def __init__(self) -> None:
        self.pedidos: list[httpx.Request] = []
        #: Respostas na ordem; a última se repete. Cada uma: (status, corpo, cabeçalhos).
        self.fila: list[tuple[int, dict, dict]] = []
        self.restante_hora = 7000

    def resposta_padrao(self, indice: int) -> tuple[int, dict, dict]:
        return (
            200,
            {
                "text": f"trecho {indice}",
                "language": "portuguese",
                "segments": [
                    {"start": 0.0, "end": 4.0, "text": f" Olá, pedaço {indice}."},
                    {"start": 4.0, "end": 9.5, "text": " Segunda frase."},
                ],
            },
            {
                "x-ratelimit-remaining-audio-seconds": str(self.restante_hora),
                "x-ratelimit-remaining-requests": "1990",
            },
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.pedidos.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [
                {"id": "whisper-large-v3-turbo", "owned_by": "OpenAI", "active": True},
                {"id": "whisper-large-v3", "owned_by": "OpenAI", "active": True},
                {"id": "llama-3.3-70b-versatile", "owned_by": "Meta", "active": True},
            ]})
        indice = sum(1 for p in self.pedidos if p.url.path.endswith("/audio/transcriptions")) - 1
        status, corpo, cab = self.fila[min(indice, len(self.fila) - 1)] if self.fila else self.resposta_padrao(indice)
        return httpx.Response(status, json=corpo, headers=cab)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def transcricoes(self) -> list[httpx.Request]:
        return [p for p in self.pedidos if p.url.path.endswith("/audio/transcriptions")]


@pytest.fixture
def fernet_key() -> str:
    return Fernet.generate_key().decode()


@pytest.fixture
def settings(monkeypatch, fernet_key):
    """Settings falsos: cofre ligado e uma GROQ_API_KEY no .env."""
    falso = SimpleNamespace(neo_cred_key=fernet_key, groq_api_key="gsk_env_1234567890abcd")
    monkeypatch.setattr("shared.config.get_settings", lambda: falso)
    config._esquecer()
    yield falso
    config._esquecer()
