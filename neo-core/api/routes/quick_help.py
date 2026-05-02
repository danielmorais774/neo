"""Quick help endpoint - respostas em streaming via LLM para perguntas pontuais."""

import os
from typing import AsyncGenerator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from openai import AsyncOpenAI
from pydantic import BaseModel

router = APIRouter(prefix="/quick-help", tags=["quick-help"])

_SYSTEM_PROMPT = (
    "Você é um assistente de ajuda rápida. Responda de forma concisa e direta.\n"
    "- Foque em respostas práticas e objetivas\n"
    "- Use markdown quando ajudar na clareza (listas, código, negrito)\n"
    "- Máximo de 4 parágrafos curtos\n"
    "- Para código, use blocos com linguagem especificada\n"
    "- Não inclua introduções longas nem repetição da pergunta"
)


class QuickHelpRequest(BaseModel):
    question: str


async def _stream_answer(question: str) -> AsyncGenerator[str, None]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        yield "Erro: OPENAI_API_KEY não configurada."
        return

    client = AsyncOpenAI(api_key=api_key)
    try:
        stream = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": question.strip()},
            ],
            max_tokens=1024,
            temperature=0.7,
            stream=True,
        )
        async for chunk in stream:
            content = chunk.choices[0].delta.content
            if content:
                yield content
    except Exception as exc:
        yield f"\n\nErro: {exc}"


@router.post("/ask")
async def ask_quick_help(request: QuickHelpRequest) -> StreamingResponse:
    """Pergunta com resposta em streaming (text/plain chunks)."""
    if not request.question.strip():
        raise HTTPException(status_code=422, detail="Pergunta não pode estar vazia")

    return StreamingResponse(
        _stream_answer(request.question),
        media_type="text/plain; charset=utf-8",
    )
