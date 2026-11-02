import { useCallback, useEffect, useState } from 'react';
import Screen from './ui/Screen';
import Icon from './ui/Icon';
import { currentUserId } from './lib/auth';

/*
 * Tela Liberações (/liberacoes) — os cards de pedido de dado.
 * Backend: neo-core/api/routes/release.py (rotas do painel, SÓ LEITURA).
 * Plano: neo-core/docs/release-request-plan.md §11 e §21.
 *
 * Por que isto existe: quando o cérebro vai entregar um dado dele a um terceiro
 * num turno que leu conteúdo de fora (página, e-mail, PDF, texto colado), o
 * portão barra e o pedido vira um card no aparelho. Até agora esse card só
 * existia no desktop/celular — se ele não estivesse na frente do aparelho, não
 * havia lugar nenhum para ver o que foi pedido, nem o que já foi decidido.
 *
 * Duas decisões desta tela, as duas de propósito:
 *
 * 1. NÃO APROVA. A decisão exige token de aparelho pareado; aceitar o painel
 *    seria afrouxar justamente a trava que faz "só eu aprovo" ser verdade — e
 *    hoje a auth das rotas roda em sombra, então quem alcançasse a :8000
 *    aprovaria sem credencial. A tela diz onde aprovar.
 * 2. O texto do card vem do CORE, lido da notificação que foi para o aparelho
 *    (`card.corpo`/`card.preview`). Reconstruir o card aqui daria duas redações
 *    do mesmo pedido, que divergem no primeiro ajuste — e o que ele aprova é um
 *    texto específico, amarrado por hash.
 */

const API = '/api/v1/liberacoes';

interface Card {
  titulo: string;
  corpo: string;
  preview: string;
}

interface Liberacao {
  id: string;
  status: 'pendente' | 'aprovada' | 'recusada' | 'expirada' | 'usada';
  origem: 'admin' | 'externo' | 'agenda';
  origem_ref: string | null;
  campos: string[];
  destino: string;
  destino_tipo: string;
  finalidade: string;
  tool: string | null;
  risco: string;
  risco_motivos: string[];
  evidencia: string;
  criado_em: string | null;
  expira_em: string | null;
  decidido_em: string | null;
  usado_em: string | null;
  nasceu_de_terceiro: boolean;
  card: Card | null;
}

const STATUS_LABEL: Record<Liberacao['status'], string> = {
  pendente: 'esperando você',
  aprovada: 'aprovada',
  recusada: 'recusada',
  expirada: 'expirou sem decisão',
  usada: 'aprovada e já usada',
};

const STATUS_COLOR: Record<Liberacao['status'], string> = {
  pendente: 'var(--neo-amber, #d9a21b)',
  aprovada: 'var(--neo-green)',
  usada: 'var(--neo-green)',
  recusada: 'var(--neo-rose)',
  expirada: 'var(--neo-text-muted)',
};

/** `pks:endereco.cep` → `endereco.cep` (o prefixo é ruído, igual no card). */
const rotulo = (campo: string) => (campo.includes(':') ? campo.split(':').slice(1).join(':') : campo);

const quando = (iso: string | null) =>
  iso ? new Date(iso).toLocaleString('pt-BR', { dateStyle: 'short', timeStyle: 'short' }) : '—';

/** Quanto falta até expirar, em texto curto. Vazio quando já passou. */
function faltam(iso: string | null): string {
  if (!iso) return '';
  const ms = new Date(iso).getTime() - Date.now();
  if (ms <= 0) return '';
  const min = Math.floor(ms / 60000);
  return min >= 60 ? `${Math.floor(min / 60)}h${String(min % 60).padStart(2, '0')}` : `${min} min`;
}

async function api<T>(url: string): Promise<T> {
  const res = await fetch(url, { headers: { 'Content-Type': 'application/json' } });
  const texto = await res.text();
  const dados = texto ? JSON.parse(texto) : {};
  if (!res.ok) throw new Error((dados as { detail?: string })?.detail ?? `HTTP ${res.status}`);
  return dados as T;
}

const LiberacoesPanel = () => {
  const [itens, setItens] = useState<Liberacao[]>([]);
  const [tudo, setTudo] = useState(false);
  const [carregando, setCarregando] = useState(true);
  const [erro, setErro] = useState<string | null>(null);
  const [aberto, setAberto] = useState<Record<string, boolean>>({});

  const carregar = useCallback(
    async (comHistorico: boolean) => {
      const dono = currentUserId();
      if (!dono) {
        setErro('Entre com seu usuário para ver as liberações deste dono.');
        setCarregando(false);
        return;
      }
      try {
        const dados = await api<{ liberacoes: Liberacao[] }>(
          `${API}?user_id=${dono}&status=${comHistorico ? 'todas' : 'pendentes'}`
        );
        setItens(dados.liberacoes ?? []);
        setErro(null);
      } catch (e: any) {
        setErro(String(e?.message ?? e));
      } finally {
        setCarregando(false);
      }
    },
    []
  );

  useEffect(() => {
    void carregar(tudo);
    // Um pendente nasce de um turno que pode estar acontecendo agora — sem
    // recarga periódica a tela mentiria por minutos.
    const t = window.setInterval(() => void carregar(tudo), 15000);
    return () => window.clearInterval(t);
  }, [carregar, tudo]);

  const pendentes = itens.filter((i) => i.status === 'pendente').length;

  const acoes = (
    <div className="neo-actions">
      <button className="neo-btn-ghost" onClick={() => void carregar(tudo)}>
        <Icon name="refresh" /> Atualizar
      </button>
      <button className={tudo ? 'neo-btn-primary' : 'neo-btn-ghost'} onClick={() => setTudo((v) => !v)}>
        {tudo ? 'Mostrando o histórico' : 'Ver histórico'}
      </button>
    </div>
  );

  return (
    <Screen icon="key" title="Liberações" actions={acoes}>
      <div className="neo-content">
        {erro && (
          <div className="neo-warn" style={{ marginBottom: 20 }}>
            {erro} <button className="neo-btn-ghost" onClick={() => void carregar(tudo)}>tentar de novo</button>
          </div>
        )}

        <div className="neo-note" style={{ marginBottom: 20 }}>
          Aqui você <strong>vê</strong> o que o Neo pediu para entregar — e o que já foi
          decidido. <strong>Aprovar é só no aparelho pareado</strong> (desktop, notch ou
          celular): a decisão exige a credencial do aparelho, e é isso que faz “só eu
          aprovo” ser verdade. Um pedido vence em minutos; depois disso ele não serve
          mais, nem se for aprovado.
        </div>

        {carregando && <p className="neo-muted">Carregando…</p>}

        {!carregando && itens.length === 0 && (
          <div className="neo-card">
            <h3 className="neo-h3">{tudo ? 'Nenhuma liberação registrada' : 'Nada esperando você'}</h3>
            <p className="neo-desc">
              Um card aparece aqui quando o cérebro tenta entregar um dado seu a um
              terceiro num turno que leu conteúdo de fora — página, e-mail, PDF ou um
              texto que você colou. Sem pedido pendente, não há nada a decidir.
            </p>
          </div>
        )}

        {!carregando && itens.length > 0 && (
          <p className="neo-meta" style={{ marginBottom: 12 }}>
            {tudo ? `${itens.length} no histórico` : `${pendentes} esperando você`}
          </p>
        )}

        {itens.map((lib) => {
          const restante = lib.status === 'pendente' ? faltam(lib.expira_em) : '';
          return (
            <div className="neo-card" key={lib.id} style={{ marginBottom: 16 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', gap: 16 }}>
                <h3 className="neo-h3" style={{ marginBottom: 4 }}>
                  Liberar {lib.campos.map(rotulo).join(', ') || '—'}?
                </h3>
                <span className="neo-meta-value" style={{ color: STATUS_COLOR[lib.status], whiteSpace: 'nowrap' }}>
                  {STATUS_LABEL[lib.status]}
                  {restante && ` · vence em ${restante}`}
                </span>
              </div>

              {/* A linha que desarma o ataque: de quem partiu o PEDIDO. Vem antes
                  da finalidade, que pode ter sido escrita pelo próprio conteúdo. */}
              {lib.origem === 'externo' && (
                <div className="neo-warn is-amber" style={{ margin: '10px 0' }}>
                  ⚠️ Este pedido veio de {lib.origem_ref || 'um terceiro'}, não de você.
                </div>
              )}
              {lib.origem === 'agenda' && (
                <div className="neo-warn is-amber" style={{ margin: '10px 0' }}>
                  ⚠️ Pedido de uma tarefa agendada, sem ninguém na frente.
                </div>
              )}
              {lib.origem === 'admin' && lib.nasceu_de_terceiro && (
                <div className="neo-warn is-amber" style={{ margin: '10px 0' }}>
                  ⚠️ Este pedido nasceu de conteúdo de terceiro neste turno, não de um
                  pedido seu — a finalidade abaixo foi escrita por esse conteúdo.
                </div>
              )}

              <div className="neo-meta" style={{ marginBottom: 6 }}>
                Destino: <span className="neo-meta-value">{lib.destino}</span>{' '}
                <span className="neo-meta-faint">({lib.destino_tipo})</span>
              </div>
              {lib.finalidade && (
                <p className="neo-desc" style={{ marginBottom: 6 }}>
                  Para: {lib.finalidade}
                </p>
              )}
              {lib.risco_motivos.length > 0 && (
                <p className="neo-meta" style={{ marginBottom: 6 }}>
                  Motivo: <span className="neo-meta-value">{lib.risco_motivos[0]}</span>
                </p>
              )}

              {lib.card?.preview && (
                <div style={{ margin: '10px 0' }}>
                  <div className="neo-meta" style={{ marginBottom: 4 }}>Texto que vai sair</div>
                  <pre className="neo-surface" style={{ whiteSpace: 'pre-wrap', padding: 12, margin: 0 }}>
                    {lib.card.preview}
                  </pre>
                </div>
              )}

              <div className="neo-meta neo-meta-faint">
                criado: {quando(lib.criado_em)}
                {lib.status === 'pendente' ? <> · vence: {quando(lib.expira_em)}</> : null}
                {lib.decidido_em ? <> · decidido: {quando(lib.decidido_em)}</> : null}
                {lib.usado_em ? <> · usado: {quando(lib.usado_em)}</> : null}
                {lib.tool ? <> · tool: {lib.tool}</> : null}
                {lib.risco ? <> · risco: {lib.risco}</> : null}
              </div>

              {(lib.evidencia || lib.card?.corpo) && (
                <button
                  className="neo-btn-ghost"
                  style={{ marginTop: 10 }}
                  onClick={() => setAberto((a) => ({ ...a, [lib.id]: !a[lib.id] }))}
                >
                  {aberto[lib.id] ? 'Esconder o original' : 'Ver o texto original e o card'}
                </button>
              )}

              {aberto[lib.id] && (
                <div style={{ marginTop: 10 }}>
                  {lib.evidencia && (
                    <>
                      <div className="neo-meta" style={{ marginBottom: 4 }}>
                        Texto que gerou o pedido
                      </div>
                      <pre className="neo-surface" style={{ whiteSpace: 'pre-wrap', padding: 12, margin: '0 0 10px' }}>
                        {lib.evidencia}
                      </pre>
                    </>
                  )}
                  {lib.card?.corpo && (
                    <>
                      <div className="neo-meta" style={{ marginBottom: 4 }}>
                        O card como chegou no aparelho
                      </div>
                      <pre className="neo-surface" style={{ whiteSpace: 'pre-wrap', padding: 12, margin: 0 }}>
                        {lib.card.corpo}
                      </pre>
                    </>
                  )}
                </div>
              )}

              {lib.status === 'pendente' && (
                <p className="neo-meta neo-meta-faint" style={{ marginTop: 10 }}>
                  Para decidir, abra o aviso no seu aparelho pareado.
                </p>
              )}
            </div>
          );
        })}
      </div>
    </Screen>
  );
};

export default LiberacoesPanel;
