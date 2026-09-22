"""Fatiamento de mensagens longas para embedding.

Contexto: o embedder local (`all-MiniLM-L6-v2`) tem `max_seq_length = 256`
tokens e trunca em SILÊNCIO. O vetor de uma mensagem longa representava só a
abertura dela — a "Reanálise Completa" da sessão 7330e40c tinha a tabela pedida
no offset 9.399 e a busca por ela devolvia 83º de 2.304.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core.memory.chunking import chunk_text
from core.session.session_manager import _flag, _int_setting


class TestChunkText:
    def test_texto_curto_sai_inteiro(self):
        assert chunk_text("oi, tudo bem?", 500, 250) == ["oi, tudo bem?"]

    def test_vazio_nao_gera_pedaco(self):
        assert chunk_text("", 500, 250) == []
        assert chunk_text("   \n  ", 500, 250) == []

    def test_fatia_texto_longo(self):
        texto = "\n\n".join(f"parágrafo {i} " + "x" * 200 for i in range(20))
        pedacos = chunk_text(texto, 500, 250)
        assert len(pedacos) > 1
        assert all(p.strip() for p in pedacos)

    def test_respeita_o_teto_de_tamanho(self):
        texto = "y" * 5000
        for p in chunk_text(texto, 500, 250):
            # +1 porque o corte natural pode incluir o separador.
            assert len(p) <= 501

    def test_cobre_o_texto_inteiro(self):
        """O fim da mensagem é justamente onde ficam tabela e conclusão."""
        texto = "\n".join(f"linha {i}" for i in range(400))
        pedacos = chunk_text(texto, 500, 250)
        assert "linha 0" in pedacos[0]
        assert "linha 399" in pedacos[-1]

    def test_sobreposicao_existe(self):
        texto = " ".join(f"palavra{i}" for i in range(400))
        pedacos = chunk_text(texto, 500, 250)
        assert len(pedacos) >= 2
        # Com 250 de 500 sobrepostos, o passo é menor que o tamanho.
        assert len(pedacos) > len(texto) // 500

    def test_overlap_maior_que_o_tamanho_nao_trava(self):
        """Um overlap mal configurado não pode fazer o laço andar para trás."""
        pedacos = chunk_text("z" * 3000, 500, 900)
        assert 1 < len(pedacos) < 100

    def test_prefere_cortar_na_quebra_de_linha(self):
        texto = ("a" * 400) + "\n\n" + ("b" * 400) + "\n\n" + ("c" * 400)
        pedacos = chunk_text(texto, 500, 100)
        # O primeiro corte cai na quebra, não no meio do bloco de "a".
        assert pedacos[0].endswith("a")


class TestSettingsTolerantes:
    """O chunking roda dentro de `add_message` — o caminho do turno.

    Um setting ausente ou mockado não pode virar TypeError e derrubar o
    salvamento da mensagem do usuário.
    """

    def test_le_valor_real(self):
        st = MagicMock()
        st.embedding_chunk_chars = 500
        assert _int_setting(st, "embedding_chunk_chars", 1200) == 500

    def test_mock_cai_no_default(self):
        """`int(MagicMock())` devolve 1 — ligaria o chunking para tudo."""
        assert _int_setting(MagicMock(), "nao_existe", 1000) == 1000

    def test_campo_ausente_cai_no_default(self):
        class Velho:
            pass

        assert _int_setting(Velho(), "embedding_chunk_chars", 500) == 500

    def test_lixo_cai_no_default(self):
        st = MagicMock()
        st.embedding_chunk_chars = "abc"
        assert _int_setting(st, "embedding_chunk_chars", 500) == 500

    def test_flag_so_aceita_bool(self):
        st = MagicMock()
        st.embedding_chunk_enabled = False
        assert _flag(st, "embedding_chunk_enabled", True) is False
        assert _flag(MagicMock(), "nao_existe", True) is True


class TestIndexMessageChunks:
    def _manager(self, **over):
        from core.session.session_manager import PersistentSessionManager

        mgr = object.__new__(PersistentSessionManager)
        st = MagicMock()
        st.embedding_chunk_enabled = over.get("enabled", True)
        st.embedding_chunk_min_chars = over.get("minimo", 1000)
        st.embedding_chunk_chars = 500
        st.embedding_chunk_overlap = 250
        mgr.settings = st
        mgr.embeddings = MagicMock()
        mgr.embeddings.available = over.get("available", True)
        mgr.db = MagicMock()
        return mgr

    @pytest.mark.asyncio
    async def test_mensagem_curta_nao_gera_pedaco(self):
        mgr = self._manager()
        assert await mgr.index_message_chunks(None, None, None, "oi") == 0

    @pytest.mark.asyncio
    async def test_flag_desligada_nao_indexa(self):
        mgr = self._manager(enabled=False)
        assert await mgr.index_message_chunks(None, None, None, "x" * 5000) == 0

    @pytest.mark.asyncio
    async def test_sem_embedder_nao_indexa(self):
        mgr = self._manager(available=False)
        assert await mgr.index_message_chunks(None, None, None, "x" * 5000) == 0

    @pytest.mark.asyncio
    async def test_falha_no_embedding_nao_propaga(self):
        """Indexar é best-effort: no pior caso a busca fica como era antes."""
        mgr = self._manager()

        async def explode(_):
            raise RuntimeError("modelo fora do ar")

        mgr.embeddings.embed_batch = explode
        assert await mgr.index_message_chunks(None, None, None, "x" * 5000) == 0
