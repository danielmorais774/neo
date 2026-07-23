"""Executor das tools fin_* — finanças pessoais do usuário (skill ``financas``).

Todas as tools exigem usuário identificado (``set_context``): o executor global
registrado no boot fica sem usuário e recusa educadamente; o cognitivo cria um
executor fresco por request (core/cognitive/read_tools.py), como devices/voice.

Saída é SEMPRE texto pt-BR compacto pensado para o modelo relatar — valores
formatados em BRL, datas dd/mm, ids curtos (8 chars) para correção.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime
from typing import Any
from uuid import UUID

from db.models.finance import (
    ACCOUNT_KINDS,
    CARD_BRANDS,
    CATEGORY_KINDS,
    FinAccount,
    FinCard,
    FinCategory,
    FinRecurring,
    FinTransaction,
    PAYMENT_METHODS,
    RECURRING_FREQUENCIES,
)
from modules.finance.money import (
    format_brl,
    invoice_due_date,
    invoice_month_for,
    month_label,
    parse_amount_cents,
    parse_date_br,
    parse_month,
    parse_month_ref,
    today_br,
)
from modules.finance.repo import FinanceRepo
from shared import ToolCall, ToolResult

logger = logging.getLogger(__name__)

_MAX_OUTPUT_CHARS = 6_000


def _short(uid: Any) -> str:
    return str(uid).replace("-", "")[:8]


def _ddmm(d: date) -> str:
    return d.strftime("%d/%m")


def _cap(text_value: str) -> str:
    if len(text_value) > _MAX_OUTPUT_CHARS:
        return text_value[:_MAX_OUTPUT_CHARS] + "\n… [saída truncada — refine o filtro]"
    return text_value


def _parse_fields(raw: Any) -> dict[str, Any]:
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(str(raw))
    except Exception as e:
        raise ValueError(f"fields não é JSON válido: {e}") from e
    if not isinstance(parsed, dict):
        raise ValueError("fields precisa ser um objeto JSON ({...}).")
    return parsed


def _validate_enum(value: str | None, allowed: tuple, label: str) -> str | None:
    if value is None or value == "":
        return None
    value = str(value).strip().lower()
    if value not in allowed:
        raise ValueError(f"{label} inválido: '{value}'. Use um de: {', '.join(allowed)}.")
    return value


def _validate_day(value: Any, label: str) -> int | None:
    if value is None or value == "":
        return None
    day = int(value)
    if not 1 <= day <= 28:
        raise ValueError(f"{label} precisa estar entre 1 e 28 (padronizamos até 28).")
    return day


def _reject_duplicate_name(existing: list, new_name: str, label: str) -> None:
    """Cadastro com nome já existente. Em prod nasceram DOIS cartões "Nubank" com 1
    minuto de diferença — e a resolução por nome passou a escolher um deles no escuro.
    """
    folded = " ".join(new_name.lower().split())
    for item in existing:
        if " ".join(str(item.name).lower().split()) == folded:
            raise ValueError(
                f"Já existe {label} com o nome '{item.name}'. Não crie outro igual — "
                f"use o que existe, ou renomeie (fin_manage action=update) se forem "
                f"mesmo coisas diferentes."
            )


def _effect_label(key: str, accounts: dict, cards: dict) -> str:
    kind, _, rest = key.partition(":")
    if kind == "conta_id":
        return f"conta {accounts.get(UUID(rest), rest)}"
    card_id, _, month = rest.partition(":")
    try:
        return f"fatura {month} do {cards.get(UUID(card_id), card_id)}"
    except ValueError:  # pragma: no cover — chave malformada nunca deveria chegar aqui
        return key


def _snap_key(effect_key: str, accounts: dict) -> str:
    """Chave do efeito → chave do snapshot (que indexa conta por NOME)."""
    if effect_key.startswith("conta_id:"):
        return f"conta:{accounts.get(UUID(effect_key.split(':', 1)[1]), '?')}"
    return effect_key


def _render_plan(
    alvos: list, efeitos: dict[str, int], antes: dict[str, int], accounts: dict, cards: dict
) -> str:
    """O que a operação vai fazer, ANTES de fazer — calculado do banco, não prometido."""
    linhas = [f"PLANO — apagar {len(alvos)} lançamento(s):"]
    for t in alvos[:12]:
        linhas.append(
            f"  · {format_brl(t.amount_cents)} {t.description} ({_ddmm(t.occurred_at)}) "
            f"[{t.kind}] id {_short(t.id)}"
        )
    if len(alvos) > 12:
        linhas.append(f"  · ... e mais {len(alvos) - 12}")
    linhas.append("Efeito esperado:")
    for key, delta in efeitos.items():
        if delta == 0:
            continue
        de = antes.get(_snap_key(key, accounts), 0)
        linhas.append(
            f"  · {_effect_label(key, accounts, cards)}: {format_brl(de)} → "
            f"{format_brl(de + delta)} ({'+' if delta > 0 else '-'}{format_brl(abs(delta))})"
        )
    return "\n".join(linhas)


def _check_effects(
    antes: dict[str, int], depois: dict[str, int], efeitos: dict[str, int],
    accounts: dict, cards: dict,
) -> str:
    """Confere no banco se o efeito previsto foi o efeito real. Divergência é gritada.

    É esta releitura que impede o "pronto, desfeito" sobre um banco que não mudou —
    o texto que o modelo recebe já vem com a verdade medida.
    """
    linhas: list[str] = ["CONFERIDO no banco (antes → depois):"]
    divergiu = False
    for key, delta in efeitos.items():
        if delta == 0:
            continue
        snap_key = _snap_key(key, accounts)
        de, para = antes.get(snap_key, 0), depois.get(snap_key, 0)
        esperado = de + delta
        ok = para == esperado
        divergiu = divergiu or not ok
        marca = "✓" if ok else "✗ DIVERGÊNCIA"
        extra = "" if ok else f" — esperado {format_brl(esperado)}"
        linhas.append(
            f"  {marca} {_effect_label(key, accounts, cards)}: {format_brl(de)} → "
            f"{format_brl(para)}{extra}"
        )
    if divergiu:
        linhas.append(
            "ATENÇÃO: o banco NÃO ficou como o plano previa. NÃO diga ao usuário que "
            "deu certo — relate a divergência."
        )
    return "\n".join(linhas)


class FinanceExecutor:
    """Executa as tools fin_* (contrato igual aos demais module executors)."""

    def __init__(self, db: Any) -> None:
        self.repo = FinanceRepo(db)
        self._user_id: UUID | None = None

    def set_context(self, user_id: UUID | None) -> None:
        self._user_id = user_id

    # -- contrato AgentExecutor ------------------------------------------------
    async def execute_tool_call(self, tool_call: ToolCall) -> ToolResult:
        start = asyncio.get_event_loop().time()
        try:
            result = await self.execute(tool_call.name, tool_call.arguments)
            return ToolResult(
                tool_call_id=tool_call.id, name=tool_call.name, result=result,
                execution_time=asyncio.get_event_loop().time() - start,
            )
        except Exception as e:
            logger.warning("finance tool '%s' failed: %s", tool_call.name, e)
            return ToolResult(
                tool_call_id=tool_call.id, name=tool_call.name, result=None,
                error=str(e),
                execution_time=asyncio.get_event_loop().time() - start,
            )

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> str:
        if self._user_id is None:
            raise ValueError("Finanças exigem um usuário identificado nesta sessão.")
        uid = self._user_id
        seed_note = await self.repo.ensure_defaults(uid)
        handlers = {
            "fin_add_transaction": self._add_transaction,
            "fin_transfer": self._transfer,
            "fin_statement": self._statement,
            "fin_card_invoice": self._card_invoice,
            "fin_balances": self._balances,
            "fin_insights": self._insights,
            "fin_manage": self._manage,
            "fin_edit_transaction": self._edit_transaction,
        }
        handler = handlers.get(tool_name)
        if handler is None:
            raise ValueError(f"Tool desconhecida no módulo finance: {tool_name}")
        result = await handler(uid, arguments)
        if seed_note:
            result = f"[{seed_note}]\n\n{result}"
        return _cap(result)

    # ── fin_add_transaction ─────────────────────────────────────────────

    async def _add_transaction(self, uid: UUID, args: dict[str, Any]) -> str:
        kind = _validate_enum(args.get("kind"), ("despesa", "receita"), "kind")
        if kind is None:
            raise ValueError("kind é obrigatório (despesa ou receita).")
        amount = parse_amount_cents(args.get("amount"))
        description = str(args.get("description") or "").strip()
        if not description:
            raise ValueError("description é obrigatória.")
        when = parse_date_br(args.get("date"))
        method = _validate_enum(args.get("payment_method"), PAYMENT_METHODS, "payment_method")
        merchant = (str(args.get("merchant") or "").strip() or None)
        notes = (str(args.get("notes") or "").strip() or None)
        installments = int(args.get("installments") or 1)

        card: FinCard | None = None
        if args.get("card"):
            card = await self.repo.resolve_card(uid, str(args["card"]))
            method = method or "credito"
        if method == "credito" and card is None:
            cards = await self.repo.list_cards(uid)
            names = ", ".join(c.name for c in cards) or "(nenhum cadastrado)"
            raise ValueError(
                f"Compra no crédito precisa do cartão. Cartões: {names}. "
                "Cadastre com fin_manage(entity=card) se for novo."
            )
        if installments > 1 and (card is None or kind != "despesa"):
            raise ValueError("Parcelamento só vale para DESPESA no cartão de crédito.")
        if installments > 48:
            raise ValueError("Máximo de 48 parcelas.")

        category: FinCategory | None = None
        category_note = ""
        if args.get("category"):
            cat_name = str(args["category"]).strip()
            try:
                category = await self.repo.resolve_category(uid, cat_name, kind=kind)
            except ValueError:
                if args.get("create_category"):
                    category = await self.repo.create(
                        FinCategory(user_id=uid, name=cat_name, kind=kind)
                    )
                    category_note = f" (categoria nova '{cat_name}' criada)"
                else:
                    raise

        account: FinAccount | None = None
        if card is None:
            if args.get("account"):
                account = await self.repo.resolve_account(uid, str(args["account"]))
            else:
                account = await self.repo.default_account(uid)
                if account is None:
                    accounts = await self.repo.list_accounts(uid)
                    names = ", ".join(a.name for a in accounts)
                    raise ValueError(
                        f"Em qual conta? Cadastradas: {names}. Passe account ou "
                        "marque uma como padrão (fin_manage)."
                    )

        cat_label = f" [{category.name}]" if category else ""
        merch_label = f" em {merchant}" if merchant else ""

        if installments > 1:
            assert card is not None
            plan, txs = await self.repo.add_installment_purchase(
                user_id=uid, card=card, total_cents=amount, n=installments,
                description=description, merchant=merchant,
                category_id=category.id if category else None,
                purchase_date=when, notes=notes,
            )
            parcela = txs[0].amount_cents
            return (
                f"Parcelado registrado: {description}{merch_label} — "
                f"{format_brl(amount)} em {installments}x de ~{format_brl(parcela)} "
                f"no {card.name}{cat_label}. 1ª parcela na fatura de "
                f"{month_label(plan.first_invoice_month)}, última em "
                f"{month_label(txs[-1].invoice_month)}.{category_note} "
                f"(id {_short(txs[0].id)})"
            )

        invoice = invoice_month_for(when, card.closing_day) if card else None
        # Mesma rede do pagamento de fatura: lançamento idêntico há instantes é quase
        # sempre o modelo repetindo a escrita, não o usuário gastando duas vezes.
        dup = await self.repo.recent_duplicate(
            uid, kind=kind, amount_cents=amount, when=when,
            card_id=card.id if card else None,
            account_id=account.id if account else None,
        )
        if dup is not None and not bool(args.get("confirm")):
            raise ValueError(
                f"JÁ EXISTE um lançamento igual: {format_brl(amount)} "
                f"({dup.description}) em {_ddmm(when)}, id {_short(dup.id)}, gravado há "
                f"instantes. Não registre de novo — se for um segundo lançamento de "
                f"verdade, repita com confirm=true."
            )
        tx = await self.repo.add_simple_transaction(
            FinTransaction(
                user_id=uid, kind=kind, amount_cents=amount, description=description,
                merchant=merchant, category_id=category.id if category else None,
                account_id=account.id if account else None,
                card_id=card.id if card else None, payment_method=method,
                occurred_at=when, invoice_month=invoice, notes=notes,
            )
        )
        where = (
            f"no cartão {card.name} (fatura {month_label(invoice)})"
            if card else f"na conta {account.name}" if account else ""
        )
        method_label = f" via {method}" if method and not card else ""
        verb = "Despesa" if kind == "despesa" else "Receita"
        return (
            f"{verb} registrada: {description}{merch_label} — {format_brl(amount)} "
            f"{where}{method_label}{cat_label}, {_ddmm(when)}.{category_note} "
            f"(id {_short(tx.id)})"
        )

    # ── fin_transfer ────────────────────────────────────────────────────

    async def _transfer(self, uid: UUID, args: dict[str, Any]) -> str:
        when = parse_date_br(args.get("date"))
        from_account = await self.repo.resolve_account(uid, str(args.get("from_account") or ""))
        confirm = bool(args.get("confirm"))

        if args.get("card"):
            card = await self.repo.resolve_card(uid, str(args["card"]))
            current_invoice = invoice_month_for(today_br(), card.closing_day)
            if args.get("invoice_month"):
                # Aceita AAAA-MM E relativo ("próximo","atual") — resolvido contra a
                # fatura CORRENTE, não a mais antiga aberta. "Adiantar o próximo mês"
                # tem que virar agosto, não pagar julho de novo (sessão 0371a5a8).
                invoice = parse_month_ref(args["invoice_month"], current=current_invoice)
            else:
                invoice = await self.repo.oldest_open_invoice(uid, card.id)
                if invoice is None:
                    invoice = current_invoice
            bal = await self.repo.invoice_balance(uid, card.id, invoice)

            # "Paguei a fatura" sem valor: o valor é a fatura, não um palpite. Antes o
            # amount era obrigatório, então o modelo INVENTAVA — pagou R$ 3.000,00 numa
            # fatura de R$ 300,00 (sessão b7fd9269).
            if args.get("amount") in (None, ""):
                if bal["aberto"] <= 0:
                    raise ValueError(
                        f"A fatura de {month_label(invoice)} do {card.name} não tem saldo "
                        f"em aberto (total {format_brl(bal['total'])}, já pago "
                        f"{format_brl(bal['pago'])}). Nada a pagar."
                    )
                amount = bal["aberto"]
            else:
                amount = parse_amount_cents(args.get("amount"))
                if amount > bal["aberto"] and not confirm:
                    raise ValueError(
                        f"Valor MAIOR que a fatura: a de {month_label(invoice)} do "
                        f"{card.name} tem {format_brl(bal['aberto'])} em aberto e você "
                        f"pediu {format_brl(amount)}. Confirme o valor com o usuário — "
                        f"se for pagamento a maior mesmo, repita com confirm=true."
                    )

            dup = await self.repo.recent_duplicate(
                uid, kind="pagamento_fatura", amount_cents=amount, when=when,
                card_id=card.id, account_id=from_account.id,
            )
            if dup is not None and not confirm:
                raise ValueError(
                    f"JÁ EXISTE este pagamento: {format_brl(amount)} do {card.name} "
                    f"em {_ddmm(when)} (id {_short(dup.id)}), gravado há instantes. Não "
                    f"pague de novo — se o usuário quer MESMO um segundo pagamento "
                    f"igual, repita com confirm=true."
                )

            description = (
                str(args.get("description") or "").strip()
                or f"Pagamento fatura {card.name} {month_label(invoice)}"
            )
            tx = await self.repo.add_simple_transaction(
                FinTransaction(
                    user_id=uid, kind="pagamento_fatura", amount_cents=amount,
                    description=description, account_id=from_account.id,
                    card_id=card.id, payment_method="pix", occurred_at=when,
                    invoice_month=invoice,
                )
            )
            restante = bal["aberto"] - amount
            sobra = (
                " Fatura quitada."
                if restante == 0
                else f" Ainda faltam {format_brl(restante)}."
                if restante > 0
                else f" Pagamento a maior: {format_brl(-restante)} de crédito no cartão."
            )
            # Outras faturas ainda abertas: deixa explícito qual mês foi pago e o que
            # sobrou — se o modelo pagou o mês errado, isto denuncia na hora.
            outras = [
                m for m, _ in await self.repo.open_invoices(uid, card.id) if m != invoice
            ]
            aviso_outras = (
                " Continuam abertas: " + ", ".join(month_label(m) for m in outras[:6]) + "."
                if outras else ""
            )
            return (
                f"Pagamento de fatura registrado: {format_brl(amount)} da conta "
                f"{from_account.name} para a fatura de {month_label(invoice)} do "
                f"{card.name}, {_ddmm(when)}.{sobra}{aviso_outras} (id {_short(tx.id)})"
            )

        amount = parse_amount_cents(args.get("amount"))

        if not args.get("to_account"):
            raise ValueError("Informe to_account (transferência) ou card (pagamento de fatura).")
        to_account = await self.repo.resolve_account(uid, str(args["to_account"]))
        if to_account.id == from_account.id:
            raise ValueError("Origem e destino são a mesma conta.")
        description = (
            str(args.get("description") or "").strip()
            or f"Transferência {from_account.name} → {to_account.name}"
        )
        await self.repo.add_transfer_pair(
            user_id=uid, amount_cents=amount, from_account=from_account,
            to_account=to_account, when=when, description=description,
        )
        return (
            f"Transferência registrada: {format_brl(amount)} de {from_account.name} "
            f"para {to_account.name}, {_ddmm(when)}."
        )

    # ── fin_statement ───────────────────────────────────────────────────

    async def _statement(self, uid: UUID, args: dict[str, Any]) -> str:
        today = today_br()
        start = parse_date_br(args.get("start_date"), default=today.replace(day=1))
        end = parse_date_br(args.get("end_date"), default=today)
        limit = min(int(args.get("limit") or 30), 100)
        account_id = card_id = None
        category_ids = None
        if args.get("account"):
            account_id = (await self.repo.resolve_account(uid, str(args["account"]))).id
        if args.get("card"):
            card_id = (await self.repo.resolve_card(uid, str(args["card"]))).id
        if args.get("category"):
            category = await self.repo.resolve_category(uid, str(args["category"]))
            category_ids = await self.repo._category_scope_ids(uid, category)
        kind = _validate_enum(
            args.get("kind"),
            ("despesa", "receita", "transf_saida", "transf_entrada", "pagamento_fatura"),
            "kind",
        )
        method = _validate_enum(args.get("payment_method"), PAYMENT_METHODS, "payment_method")

        rows, totals = await self.repo.statement(
            uid, start=start, end=end, account_id=account_id, card_id=card_id,
            category_ids=category_ids, kind=kind, payment_method=method,
            search=(str(args.get("search") or "").strip() or None), limit=limit,
        )
        if not rows:
            return f"Nenhum lançamento entre {_ddmm(start)} e {_ddmm(end)} com esses filtros."

        sign = {"despesa": "-", "receita": "+", "transf_saida": "-",
                "transf_entrada": "+", "pagamento_fatura": "-"}
        lines = []
        for r in rows:
            parts = [
                _ddmm(r["occurred_at"]),
                f"{sign.get(r['kind'], '')}{format_brl(r['amount_cents'])}",
                r["description"],
            ]
            if r["merchant"]:
                parts.append(f"@{r['merchant']}")
            if r["category"]:
                parts.append(f"[{r['category']}]")
            # Pagamento de fatura: a origem do dinheiro é a CONTA (o cartão já
            # aparece na descrição); compra no crédito mostra o cartão.
            if r["kind"] == "pagamento_fatura":
                origin = r["account"] or r["card"]
            else:
                origin = r["card"] or r["account"]
            if origin:
                parts.append(origin)
            if r["payment_method"]:
                parts.append(r["payment_method"])
            if r["installment_no"] and r["n_installments"]:
                parts.append(f"parc {r['installment_no']}/{r['n_installments']}")
            parts.append(f"id {_short(r['id'])}")
            lines.append(" | ".join(parts))

        n = int(totals.get("n") or 0)
        entrada = int(totals.get("entrada_conta") or 0)
        saida = int(totals.get("saida_conta") or 0)
        credito = int(totals.get("credito") or 0)
        header = (
            f"Extrato {_ddmm(start)}–{_ddmm(end)} — {n} lançamento(s)"
            + (f" (mostrando {len(rows)})" if n > len(rows) else "")
        )
        # Rodapé de CAIXA: entrou/saiu da conta e o líquido — que BATE com o saldo
        # (inclui pagamento de fatura). O antigo "resultado = receitas − despesas"
        # contava a compra no crédito e ignorava os pagamentos de fatura, dando um
        # número (6700) que não era nem o saldo (6400) nem coisa nenhuma (b2a0ecf9).
        footer = (
            f"Movimento em conta: entrou {format_brl(entrada)} · saiu "
            f"{format_brl(saida)} · líquido {format_brl(entrada - saida)}"
        )
        if credito:
            footer += (
                f"\nCompras no CRÉDITO no período: {format_brl(credito)} — entram na "
                f"fatura, NÃO saem da conta agora (só quando você paga a fatura, e aí "
                f"já contam no 'saiu' acima)."
            )
        return header + "\n" + "\n".join(lines) + "\n" + footer

    # ── fin_card_invoice ────────────────────────────────────────────────

    async def _card_invoice(self, uid: UUID, args: dict[str, Any]) -> str:
        card = await self.repo.resolve_card(uid, str(args.get("card") or ""))
        today = today_br()
        default_month = invoice_month_for(today, card.closing_day)
        # parse_month_ref (não parse_month): aceita "proximo"/"agosto" como no
        # fin_transfer. O cru crashava com "Data inválida: 'proximo'" (sessão b2a0ecf9).
        month = (
            parse_month_ref(args["month"], current=default_month)
            if args.get("month") else default_month
        )
        data = await self.repo.card_invoice(uid, card, month)

        total = data["total_cents"]
        paid = data["paid_cents"]
        aberto = total - paid
        due = invoice_due_date(month, card.closing_day, card.due_day)
        vence = f" · vence {due.strftime('%d/%m/%Y')}" if due else ""

        # Mostra a conta da fatura por extenso (compras − pago = em aberto) em vez de
        # só "PAGA": o pagamento é um CRÉDITO na fatura que zera as compras, e ver
        # isso escrito evita a dúvida "por que a fatura lê 0?" (sessão b2a0ecf9).
        if total == 0 and not data["items"]:
            head = f"Fatura {month_label(month)} do {card.name}: sem lançamentos{vence}"
        elif paid > 0:
            nota = " (quitada)" if aberto <= 0 else ""
            head = (
                f"Fatura {month_label(month)} do {card.name}: "
                f"{format_brl(total)} em compras − {format_brl(paid)} pago (crédito) = "
                f"{format_brl(aberto)} em aberto{nota}{vence}"
            )
        else:
            head = (
                f"Fatura {month_label(month)} do {card.name}: "
                f"{format_brl(aberto)} em aberto{vence}"
            )
        lines = [head]
        for item in data["items"][:40]:
            parc = (
                f" ({item['installment_no']}/{item['n_installments']})"
                if item["installment_no"] and item["n_installments"] else ""
            )
            cat = f" [{item['category']}]" if item["category"] else ""
            merch = f" @{item['merchant']}" if item["merchant"] else ""
            lines.append(
                f"- {_ddmm(item['occurred_at'])} {format_brl(item['amount_cents'])} "
                f"{item['description']}{parc}{merch}{cat} (id {_short(item['id'])})"
            )
        if len(data["items"]) > 40:
            lines.append(f"… +{len(data['items']) - 40} itens (use fin_statement para ver tudo)")
        if card.limit_cents:
            available = card.limit_cents - data["debt_cents"]
            lines.append(
                f"Limite: {format_brl(card.limit_cents)} · usado {format_brl(data['debt_cents'])} "
                f"· disponível {format_brl(available)}"
            )
        if data["future"]:
            future_str = ", ".join(
                f"{month_label(f['invoice_month'])} {format_brl(f['total'])} ({f['n']} itens)"
                for f in data["future"][:6]
            )
            lines.append(f"Próximas faturas já comprometidas: {future_str}")
        return "\n".join(lines)

    # ── fin_balances ────────────────────────────────────────────────────

    async def _balances(self, uid: UUID, args: dict[str, Any]) -> str:
        today = today_br()
        data = await self.repo.balances(uid, today=today)
        lines = ["Contas:"]
        total = 0
        for a in data["accounts"]:
            balance = int(a["balance_cents"])
            total += balance
            default_mark = " (padrão)" if a["is_default"] else ""
            lines.append(f"- {a['name']} ({a['kind']}){default_mark}: {format_brl(balance)}")
        lines.append(f"Total em contas: {format_brl(total)}")
        if data["cards"]:
            lines.append("\nCartões:")
            for c in data["cards"]:
                card = c["card"]
                extra = ""
                if card.limit_cents:
                    extra = f" · disponível {format_brl(card.limit_cents - c['debt_cents'])}"
                lines.append(
                    f"- {card.name}: fatura {month_label(c['current_invoice_month'])} "
                    f"{format_brl(c['current_invoice_cents'])} · saldo devedor total "
                    f"{format_brl(c['debt_cents'])}{extra}"
                )
        month = data["month"]
        despesas = int(month.get("despesas") or 0)
        receitas = int(month.get("receitas") or 0)
        lines.append(
            f"\nMês atual até {_ddmm(today)}: receitas {format_brl(receitas)} · "
            f"despesas {format_brl(despesas)} · resultado {format_brl(receitas - despesas)}"
        )
        return "\n".join(lines)

    # ── fin_insights ────────────────────────────────────────────────────

    async def _insights(self, uid: UUID, args: dict[str, Any]) -> str:
        today = today_br()
        start = parse_date_br(args.get("start_date"), default=today.replace(day=1))
        end = parse_date_br(args.get("end_date"), default=today)
        compare = args.get("compare")
        compare = True if compare is None else bool(compare)
        data = await self.repo.insights(
            uid, start=start, end=end, compare=compare, today=today
        )

        despesas = int(data["totals"].get("despesas") or 0)
        receitas = int(data["totals"].get("receitas") or 0)
        days = max((end - start).days + 1, 1)
        prev_start, prev_end = data["prev_range"]

        lines = [
            f"Insights {_ddmm(start)}–{_ddmm(end)}"
            + (f" (comparando com {_ddmm(prev_start)}–{_ddmm(prev_end)})" if compare else "")
        ]
        lines.append(
            f"Despesas {format_brl(despesas)} · Receitas {format_brl(receitas)} · "
            f"Resultado {format_brl(receitas - despesas)} · Média diária de gasto "
            f"{format_brl(despesas // days)}"
        )

        # Rollup por grupo (pai), com detalhe das filhas e Δ vs período anterior.
        groups: dict[str, int] = {}
        detail: dict[str, list[tuple[str, int]]] = {}
        for row in data["by_group"]:
            group = row["grupo"]
            groups[group] = groups.get(group, 0) + int(row["total"])
            if row["categoria"] != group:
                detail.setdefault(group, []).append((row["categoria"], int(row["total"])))
        prev_groups: dict[str, int] = {}
        for row in data["prev_by_group"]:
            prev_groups[row["grupo"]] = prev_groups.get(row["grupo"], 0) + int(row["total"])

        if groups:
            lines.append("\nDespesas por categoria:")
            for group, value in sorted(groups.items(), key=lambda kv: -kv[1])[:12]:
                pct = round(100 * value / despesas) if despesas else 0
                delta = ""
                if compare and group in prev_groups and prev_groups[group] > 0:
                    change = round(100 * (value - prev_groups[group]) / prev_groups[group])
                    delta = f" ({'+' if change >= 0 else ''}{change}% vs anterior)"
                elif compare and group not in prev_groups:
                    delta = " (novo)"
                children = ""
                if detail.get(group):
                    top_children = sorted(detail[group], key=lambda kv: -kv[1])[:3]
                    children = " — " + ", ".join(
                        f"{name} {format_brl(v)}" for name, v in top_children
                    )
                lines.append(f"- {group}: {format_brl(value)} ({pct}%){delta}{children}")

        if data["merchants"]:
            tops = ", ".join(
                f"{m['merchant']} {format_brl(int(m['total']))} ({m['n']}x)"
                for m in data["merchants"]
            )
            lines.append(f"\nTop estabelecimentos: {tops}")
        if data["methods"]:
            methods = " · ".join(
                f"{m['metodo']} {format_brl(int(m['total']))}" for m in data["methods"]
            )
            lines.append(f"Por método: {methods}")
        if len(data["trend"]) > 1:
            trend = " | ".join(
                f"{month_label(t['mes'])} -{format_brl(int(t['despesas']))}"
                f"/+{format_brl(int(t['receitas']))}"
                for t in data["trend"]
            )
            lines.append(f"Evolução (despesa/receita): {trend}")
        if data["future_installments"]:
            future = ", ".join(
                f"{month_label(f['invoice_month'])} {format_brl(int(f['total']))} ({f['n']} parc.)"
                for f in data["future_installments"]
            )
            lines.append(f"Parcelas futuras comprometidas: {future}")
        recurrings = data["recurrings"]
        if recurrings:
            fixed = sum(r.amount_cents or 0 for r in recurrings if r.kind == "despesa")
            names = ", ".join(
                f"{r.description}"
                + (f" {format_brl(r.amount_cents)}" if r.amount_cents else " (variável)")
                for r in recurrings[:10]
            )
            lines.append(
                f"Recorrências ativas (~{format_brl(fixed)}/mês fixo): {names}"
            )

        if data["budgets"]:
            spent = await self.repo.spent_by_category_tree(uid, start=start, end=end)
            lines.append("\nOrçamentos:")
            for b in data["budgets"]:
                target = int(b["amount_cents"])
                if b["category_id"] is None:
                    used = despesas
                    label = "Geral"
                else:
                    used = spent.get(str(b["category_id"]), 0)
                    label = b["category"] or "?"
                pct = round(100 * used / target) if target else 0
                flag = " ⚠ ESTOUROU" if used > target else ""
                lines.append(
                    f"- {label}: {format_brl(used)} de {format_brl(target)} ({pct}%){flag}"
                )
        return "\n".join(lines)

    # ── fin_manage ──────────────────────────────────────────────────────

    async def _manage(self, uid: UUID, args: dict[str, Any]) -> str:
        entity = str(args.get("entity") or "").strip().lower()
        action = str(args.get("action") or "").strip().lower()
        fields = _parse_fields(args.get("fields"))
        name = str(args.get("name") or "").strip()
        dispatch = {
            "account": self._manage_account,
            "card": self._manage_card,
            "category": self._manage_category,
            "budget": self._manage_budget,
            "recurring": self._manage_recurring,
        }
        if entity not in dispatch:
            raise ValueError(f"entity inválida: '{entity}'.")
        if action not in ("create", "update", "archive", "list"):
            raise ValueError(f"action inválida: '{action}'.")
        return await dispatch[entity](uid, action, name, fields)

    async def _manage_account(self, uid: UUID, action: str, name: str, f: dict) -> str:
        if action == "list":
            accounts = await self.repo.list_accounts(uid)
            if not accounts:
                return "Nenhuma conta cadastrada."
            return "Contas:\n" + "\n".join(
                f"- {a.name} ({a.kind}"
                + (f", {a.institution}" if a.institution else "")
                + (", padrão" if a.is_default else "") + ")"
                for a in accounts
            )
        if action == "create":
            new_name = str(f.get("name") or name or "").strip()
            if not new_name:
                raise ValueError("Passe o nome da conta em fields.name.")
            _reject_duplicate_name(await self.repo.list_accounts(uid), new_name, "conta")
            kind = _validate_enum(f.get("kind"), ACCOUNT_KINDS, "kind") or "corrente"
            initial = parse_amount_cents(f["initial_balance"]) if f.get("initial_balance") else 0
            account = await self.repo.create(FinAccount(
                user_id=uid, name=new_name, kind=kind,
                institution=(str(f.get("institution") or "").strip() or None),
                initial_balance_cents=initial,
            ))
            if f.get("is_default"):
                await self.repo.set_default_account(uid, account.id)
            return (
                f"Conta '{new_name}' ({kind}) criada"
                + (f" com saldo inicial {format_brl(initial)}" if initial else "")
                + ("; agora é a conta padrão." if f.get("is_default") else ".")
            )
        account = await self.repo.resolve_account(uid, name or str(f.get("name") or ""))
        if action == "archive":
            await self.repo.apply_update(FinAccount, account.id, {"archived_at": datetime.utcnow()})
            return f"Conta '{account.name}' arquivada (o histórico continua no extrato)."
        changes: dict[str, Any] = {}
        if f.get("new_name"):
            changes["name"] = str(f["new_name"]).strip()
        if f.get("kind"):
            changes["kind"] = _validate_enum(f["kind"], ACCOUNT_KINDS, "kind")
        if "institution" in f:
            changes["institution"] = str(f["institution"] or "").strip() or None
        if f.get("initial_balance") is not None and f.get("initial_balance") != "":
            changes["initial_balance_cents"] = parse_amount_cents(f["initial_balance"])
        if changes:
            await self.repo.apply_update(FinAccount, account.id, changes)
        if f.get("is_default"):
            await self.repo.set_default_account(uid, account.id)
            changes["is_default"] = True
        if not changes:
            raise ValueError(
                "Nada para alterar. Campos: new_name, kind, institution, "
                "initial_balance, is_default."
            )
        return f"Conta '{account.name}' atualizada ({', '.join(changes)})."

    async def _manage_card(self, uid: UUID, action: str, name: str, f: dict) -> str:
        if action == "list":
            cards = await self.repo.list_cards(uid)
            if not cards:
                return "Nenhum cartão cadastrado."
            lines = []
            for c in cards:
                bits = [c.brand or "", f"final {c.last4}" if c.last4 else ""]
                if c.limit_cents:
                    bits.append(f"limite {format_brl(c.limit_cents)}")
                if c.closing_day:
                    bits.append(f"fecha dia {c.closing_day}")
                if c.due_day:
                    bits.append(f"vence dia {c.due_day}")
                detail = ", ".join(b for b in bits if b)
                # id na listagem: com nomes repetidos, é o único jeito de o usuário
                # dizer QUAL cartão (e de desfazer o cadastro duplicado).
                lines.append(
                    f"- {c.name}" + (f" ({detail})" if detail else "") + f" — id {_short(c.id)}"
                )
            return "Cartões:\n" + "\n".join(lines)
        if action == "create":
            new_name = str(f.get("name") or name or "").strip()
            if not new_name:
                raise ValueError("Passe o nome do cartão em fields.name.")
            _reject_duplicate_name(await self.repo.list_cards(uid), new_name, "cartão")
            account_id = None
            if f.get("account"):
                account_id = (await self.repo.resolve_account(uid, str(f["account"]))).id
            card = await self.repo.create(FinCard(
                user_id=uid, name=new_name,
                brand=_validate_enum(f.get("brand"), CARD_BRANDS, "brand"),
                last4=(str(f.get("last4") or "").strip()[-4:] or None),
                limit_cents=parse_amount_cents(f["limit"]) if f.get("limit") else None,
                closing_day=_validate_day(f.get("closing_day"), "closing_day"),
                due_day=_validate_day(f.get("due_day"), "due_day"),
                account_id=account_id,
            ))
            missing = []
            if card.closing_day is None:
                missing.append("dia de fechamento")
            if card.due_day is None:
                missing.append("dia de vencimento")
            hint = (
                f" Falta o {' e o '.join(missing)} — sem isso a fatura assume o mês civil."
                if missing else ""
            )
            return f"Cartão '{new_name}' criado.{hint}"
        card = await self.repo.resolve_card(uid, name or str(f.get("name") or ""))
        if action == "archive":
            await self.repo.apply_update(FinCard, card.id, {"archived_at": datetime.utcnow()})
            return f"Cartão '{card.name}' arquivado."
        changes: dict[str, Any] = {}
        if f.get("new_name"):
            changes["name"] = str(f["new_name"]).strip()
        if f.get("brand"):
            changes["brand"] = _validate_enum(f["brand"], CARD_BRANDS, "brand")
        if f.get("last4"):
            changes["last4"] = str(f["last4"]).strip()[-4:]
        if f.get("limit"):
            changes["limit_cents"] = parse_amount_cents(f["limit"])
        if f.get("closing_day") is not None and f.get("closing_day") != "":
            changes["closing_day"] = _validate_day(f["closing_day"], "closing_day")
        if f.get("due_day") is not None and f.get("due_day") != "":
            changes["due_day"] = _validate_day(f["due_day"], "due_day")
        if f.get("account"):
            changes["account_id"] = (await self.repo.resolve_account(uid, str(f["account"]))).id
        if not changes:
            raise ValueError(
                "Nada para alterar. Campos: new_name, brand, last4, limit, "
                "closing_day, due_day, account."
            )
        await self.repo.apply_update(FinCard, card.id, changes)
        return f"Cartão '{card.name}' atualizado ({', '.join(changes)})."

    async def _manage_category(self, uid: UUID, action: str, name: str, f: dict) -> str:
        if action == "list":
            categories = await self.repo.list_categories(uid)
            by_kind: dict[str, list[str]] = {"despesa": [], "receita": []}
            children: dict[Any, list[str]] = {}
            for c in categories:
                if c.parent_id:
                    children.setdefault(c.parent_id, []).append(c.name)
            for c in categories:
                if c.parent_id is None:
                    subs = children.get(c.id)
                    by_kind[c.kind].append(
                        c.name + (f" ({', '.join(sorted(subs))})" if subs else "")
                    )
            return (
                "Categorias de despesa:\n- " + "\n- ".join(by_kind["despesa"])
                + "\n\nCategorias de receita:\n- " + "\n- ".join(by_kind["receita"])
            )
        if action == "create":
            new_name = str(f.get("name") or name or "").strip()
            if not new_name:
                raise ValueError("Passe o nome da categoria em fields.name.")
            kind = _validate_enum(f.get("kind"), CATEGORY_KINDS, "kind") or "despesa"
            parent_id = None
            if f.get("parent"):
                parent = await self.repo.resolve_category(uid, str(f["parent"]), kind=kind)
                parent_id = parent.id if parent.parent_id is None else parent.parent_id
            await self.repo.create(FinCategory(
                user_id=uid, name=new_name, kind=kind, parent_id=parent_id,
            ))
            return f"Categoria '{new_name}' ({kind}) criada."
        category = await self.repo.resolve_category(uid, name or str(f.get("name") or ""))
        if action == "archive":
            await self.repo.apply_update(
                FinCategory, category.id, {"archived_at": datetime.utcnow()}
            )
            return f"Categoria '{category.name}' arquivada (lançamentos antigos a mantêm)."
        changes: dict[str, Any] = {}
        if f.get("new_name"):
            changes["name"] = str(f["new_name"]).strip()
        if f.get("parent"):
            parent = await self.repo.resolve_category(uid, str(f["parent"]), kind=category.kind)
            changes["parent_id"] = parent.id if parent.parent_id is None else parent.parent_id
        if not changes:
            raise ValueError("Nada para alterar. Campos: new_name, parent.")
        await self.repo.apply_update(FinCategory, category.id, changes)
        return f"Categoria '{category.name}' atualizada."

    async def _manage_budget(self, uid: UUID, action: str, name: str, f: dict) -> str:
        if action == "list":
            budgets = await self.repo.list_budgets(uid)
            if not budgets:
                return "Nenhum orçamento definido."
            categories = {c.id: c.name for c in await self.repo.list_categories(uid)}
            return "Orçamentos:\n" + "\n".join(
                f"- {categories.get(b.category_id, 'Geral')}: {format_brl(b.amount_cents)}"
                + (f" (só {month_label(b.month)})" if b.month else " (todo mês)")
                for b in budgets
            )
        category_name = str(f.get("category") or name or "").strip()
        category = (
            await self.repo.resolve_category(uid, category_name, kind="despesa")
            if category_name and category_name.lower() not in ("geral", "global", "total")
            else None
        )
        if action == "archive":
            budgets = await self.repo.list_budgets(uid)
            target_cat = category.id if category else None
            for b in budgets:
                if b.category_id == target_cat:
                    await self.repo.delete_budget(b.id)
                    return f"Orçamento de {category.name if category else 'Geral'} removido."
            raise ValueError("Não achei esse orçamento (fin_manage budget list para ver).")
        # create/update = upsert
        if not f.get("amount"):
            raise ValueError("Passe fields.amount (valor mensal em reais).")
        month = parse_month(f["month"]) if f.get("month") else None
        budget, created = await self.repo.upsert_budget(
            uid, category_id=category.id if category else None, month=month,
            amount_cents=parse_amount_cents(f["amount"]),
        )
        scope = category.name if category else "Geral (todas as despesas)"
        when = f" para {month_label(month)}" if month else " (todo mês)"
        return (
            f"Orçamento {'criado' if created else 'atualizado'}: {scope} — "
            f"{format_brl(budget.amount_cents)}{when}."
        )

    async def _manage_recurring(self, uid: UUID, action: str, name: str, f: dict) -> str:
        if action == "list":
            recurrings = await self.repo.list_recurrings(uid, only_active=False)
            if not recurrings:
                return "Nenhuma recorrência cadastrada."
            lines = []
            for r in recurrings:
                bits = [
                    format_brl(r.amount_cents) if r.amount_cents else "valor variável",
                    r.frequency,
                ]
                if r.due_day:
                    bits.append(f"dia {r.due_day}")
                if not r.active:
                    bits.append("INATIVA")
                lines.append(f"- {r.description} ({', '.join(bits)})")
            return "Recorrências (assinaturas/contas fixas):\n" + "\n".join(lines)
        if action == "create":
            description = str(f.get("description") or f.get("name") or name or "").strip()
            if not description:
                raise ValueError("Passe fields.description (ex: 'Netflix', 'Aluguel').")
            kind = _validate_enum(f.get("kind"), CATEGORY_KINDS, "kind") or "despesa"
            category_id = None
            if f.get("category"):
                category_id = (
                    await self.repo.resolve_category(uid, str(f["category"]), kind=kind)
                ).id
            account_id = card_id = None
            if f.get("account"):
                account_id = (await self.repo.resolve_account(uid, str(f["account"]))).id
            if f.get("card"):
                card_id = (await self.repo.resolve_card(uid, str(f["card"]))).id
            recurring = await self.repo.create(FinRecurring(
                user_id=uid, description=description, kind=kind,
                amount_cents=parse_amount_cents(f["amount"]) if f.get("amount") else None,
                category_id=category_id,
                payment_method=_validate_enum(
                    f.get("payment_method"), PAYMENT_METHODS, "payment_method"
                ),
                account_id=account_id, card_id=card_id,
                due_day=_validate_day(f.get("due_day") or f.get("day"), "due_day"),
                frequency=_validate_enum(
                    f.get("frequency"), RECURRING_FREQUENCIES, "frequency"
                ) or "mensal",
                notes=(str(f.get("notes") or "").strip() or None),
            ))
            value = format_brl(recurring.amount_cents) if recurring.amount_cents else "variável"
            return (
                f"Recorrência '{description}' cadastrada ({value}, {recurring.frequency}"
                + (f", dia {recurring.due_day}" if recurring.due_day else "") + "). "
                "Lembrete: o lançamento de cada mês continua sendo registrado "
                "normalmente (fin_add_transaction)."
            )
        recurrings = await self.repo.list_recurrings(uid, only_active=False)
        target = self.repo._match_by_name(
            [r for r in recurrings], name or str(f.get("description") or f.get("name") or ""),
            label="recorrência",
        ) if recurrings else None
        if target is None:
            raise ValueError("Nenhuma recorrência cadastrada.")
        if action == "archive":
            await self.repo.apply_update(FinRecurring, target.id, {"active": False})
            return f"Recorrência '{target.description}' desativada."
        changes: dict[str, Any] = {}
        if f.get("amount"):
            changes["amount_cents"] = parse_amount_cents(f["amount"])
        if f.get("due_day") or f.get("day"):
            changes["due_day"] = _validate_day(f.get("due_day") or f.get("day"), "due_day")
        if f.get("frequency"):
            changes["frequency"] = _validate_enum(f["frequency"], RECURRING_FREQUENCIES, "frequency")
        if f.get("active") is not None:
            changes["active"] = bool(f["active"])
        if f.get("new_name") or f.get("new_description"):
            changes["description"] = str(f.get("new_name") or f.get("new_description")).strip()
        if not changes:
            raise ValueError(
                "Nada para alterar. Campos: amount, due_day, frequency, active, new_name."
            )
        await self.repo.apply_update(FinRecurring, target.id, changes)
        return f"Recorrência '{target.description}' atualizada ({', '.join(changes)})."

    # ── fin_edit_transaction ────────────────────────────────────────────

    async def _resolve_target(self, uid: UUID, args: dict[str, Any]) -> FinTransaction:
        """Acha o lançamento por id/'ultimo' OU por descrição do alvo (target)."""
        if args.get("transaction_id"):
            return await self.repo.find_transaction(uid, str(args["transaction_id"]))
        raw = _parse_fields(args.get("target"))
        if not raw:
            raise ValueError(
                "Diga QUAL lançamento: transaction_id (id ou 'ultimo') ou target "
                "com o que você sabe dele, ex.: "
                '{"kind":"pagamento_fatura","card":"Nubank","invoice_month":"2026-07"}.'
            )
        target: dict[str, Any] = {}
        card = None
        if raw.get("kind"):
            target["kind"] = _validate_enum(
                raw["kind"], ("despesa", "receita", "pagamento_fatura"), "target.kind"
            )
        if raw.get("card"):
            card = await self.repo.resolve_card(uid, str(raw["card"]))
            target["card_id"] = card.id
        if raw.get("account"):
            target["account_id"] = (await self.repo.resolve_account(uid, str(raw["account"]))).id
        if raw.get("invoice_month"):
            base = invoice_month_for(today_br(), card.closing_day if card else None)
            target["invoice_month"] = parse_month_ref(raw["invoice_month"], current=base)
        if raw.get("amount"):
            target["amount_cents"] = parse_amount_cents(raw["amount"])
        if raw.get("date"):
            target["date"] = parse_date_br(raw["date"])
        if raw.get("description"):
            target["description"] = str(raw["description"]).strip()

        found = await self.repo.select_transactions(uid, target)
        if not found:
            raise ValueError("Nenhum lançamento casa com esse alvo. Confira no extrato.")
        if len(found) > 1:
            linhas = "\n".join(
                f"- {format_brl(t.amount_cents)} {t.description} ({_ddmm(t.occurred_at)}) "
                f"id {_short(t.id)}"
                for t in found[:8]
            )
            raise ValueError(
                f"{len(found)} lançamentos casam com esse alvo — diga QUAL (passe o id):\n{linhas}"
            )
        return found[0]

    def _delete_effects(self, txs: list[FinTransaction]) -> dict[str, int]:
        """Efeito ARITMÉTICO de apagar estes lançamentos (o que o plano promete).

        Apagar um lançamento desfaz o sinal dele: pagamento de fatura e despesa
        devolvem dinheiro à conta e reabrem a fatura; receita tira.
        """
        effects: dict[str, int] = {}
        for tx in txs:
            if tx.account_id is not None:
                key = f"conta_id:{tx.account_id}"
                delta = {
                    "receita": -tx.amount_cents, "transf_entrada": -tx.amount_cents,
                    "despesa": tx.amount_cents, "transf_saida": tx.amount_cents,
                    "pagamento_fatura": tx.amount_cents,
                }.get(tx.kind, 0)
                effects[key] = effects.get(key, 0) + delta
            if tx.card_id is not None and tx.invoice_month is not None:
                key = f"fatura:{tx.card_id}:{tx.invoice_month:%Y-%m}"
                delta = (
                    -tx.amount_cents if tx.kind == "despesa"
                    else tx.amount_cents if tx.kind == "pagamento_fatura"
                    else 0
                )
                effects[key] = effects.get(key, 0) + delta
        return effects

    async def _edit_transaction(self, uid: UUID, args: dict[str, Any]) -> str:
        tx = await self._resolve_target(uid, args)
        action = str(args.get("action") or "").strip().lower()
        label = f"{tx.description} de {format_brl(tx.amount_cents)} ({_ddmm(tx.occurred_at)})"
        preview = bool(args.get("preview"))

        if action == "delete":
            whole_plan = str(args.get("scope") or "").strip().lower() == "plano"
            alvos = await self.repo.plan_delete_targets(uid, tx, whole_plan=whole_plan)
            efeitos = self._delete_effects(alvos)
            invoices = sorted({
                (t.card_id, t.invoice_month) for t in alvos
                if t.card_id and t.invoice_month
            })
            accounts = {a.id: a.name for a in await self.repo.list_accounts(uid)}
            cards = {c.id: c.name for c in await self.repo.list_cards(uid)}
            today = today_br()
            antes = await self.repo.ledger_snapshot(uid, today=today, invoices=invoices)

            plano = _render_plan(alvos, efeitos, antes, accounts, cards)
            if preview:
                return plano + "\nNADA FOI EXECUTADO (preview). Confirme com o usuário e repita sem preview."

            count = await self.repo.soft_delete_transaction(uid, tx, whole_plan=whole_plan)
            depois = await self.repo.ledger_snapshot(uid, today=today, invoices=invoices)
            conferencia = _check_effects(antes, depois, efeitos, accounts, cards)
            cabeca = (
                f"Plano de parcelamento excluído: {label} — {count} parcela(s) removida(s)."
                if tx.installment_plan_id and whole_plan
                else f"Transferência excluída (as duas pernas): {label}."
                if tx.transfer_group
                else (
                    f"Parcela excluída: {label}. As demais parcelas do plano continuam "
                    "— use scope=plano para cancelar tudo."
                )
                if tx.installment_plan_id
                else f"Lançamento excluído: {label}."
            )
            return f"{cabeca}\n\n{conferencia}"

        if action != "update":
            raise ValueError("action precisa ser update ou delete.")
        f = _parse_fields(args.get("fields"))
        if not f:
            raise ValueError(
                "Passe fields com o que mudar: amount, description, category, date, "
                "merchant, account, card, payment_method, notes."
            )
        changes: dict[str, Any] = {}
        if f.get("amount"):
            changes["amount_cents"] = parse_amount_cents(f["amount"])
        if f.get("description"):
            changes["description"] = str(f["description"]).strip()
        if f.get("merchant") is not None:
            changes["merchant"] = str(f["merchant"] or "").strip() or None
        if f.get("notes") is not None:
            changes["notes"] = str(f["notes"] or "").strip() or None
        if f.get("category"):
            kind_hint = tx.kind if tx.kind in ("despesa", "receita") else None
            category = await self.repo.resolve_category(uid, str(f["category"]), kind=kind_hint)
            changes["category_id"] = category.id
        if f.get("account"):
            account = await self.repo.resolve_account(uid, str(f["account"]))
            changes["account_id"] = account.id
        if f.get("payment_method"):
            changes["payment_method"] = _validate_enum(
                f["payment_method"], PAYMENT_METHODS, "payment_method"
            )
        card = None
        if f.get("card"):
            card = await self.repo.resolve_card(uid, str(f["card"]))
            changes["card_id"] = card.id
            changes["account_id"] = None
            changes["payment_method"] = "credito"
        if f.get("date"):
            new_date = parse_date_br(f["date"])
            changes["occurred_at"] = new_date
            # Recalcula a fatura só para compra de crédito avulsa — parcela mantém
            # a fatura do plano (senão desalinha das irmãs).
            effective_card_id = changes.get("card_id", tx.card_id)
            if effective_card_id and not tx.installment_plan_id:
                the_card = card or next(
                    (c for c in await self.repo.list_cards(uid) if c.id == effective_card_id),
                    None,
                )
                if the_card is not None:
                    changes["invoice_month"] = invoice_month_for(new_date, the_card.closing_day)
        elif card is not None and not tx.installment_plan_id:
            changes["invoice_month"] = invoice_month_for(tx.occurred_at, card.closing_day)
        if tx.installment_plan_id and ("amount_cents" in changes):
            raise ValueError(
                "Não altero valor de UMA parcela (desalinharia o plano). Exclua o "
                "plano (delete + scope=plano) e registre de novo com o valor certo."
            )
        await self.repo.apply_update(FinTransaction, tx.id, changes)
        return f"Lançamento atualizado ({', '.join(changes)}): {label}."
