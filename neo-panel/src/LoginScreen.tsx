// Tela de login do painel (neo-core/docs/api-auth-plan.md §5).
//
// Existe porque o painel autenticava com um token estático que não dizia quem
// estava olhando. Sem uma pessoa identificada, o painel não podia aprovar
// liberação de dado nem responder "de quem é esta conversa" — e o `user_id` das
// telas era um UUID fixo escrito no fonte.

import { useState } from 'react';
import { login } from './lib/auth';

interface Props {
  /** Texto do motivo, quando a sessão caiu em vez de nunca ter existido. */
  aviso?: string | null;
  onEntrou: () => void;
}

export default function LoginScreen({ aviso, onEntrou }: Props) {
  const [usuario, setUsuario] = useState('');
  const [senha, setSenha] = useState('');
  const [erro, setErro] = useState<string | null>(null);
  const [enviando, setEnviando] = useState(false);

  const enviar = async (e: React.FormEvent) => {
    e.preventDefault();
    setErro(null);
    setEnviando(true);
    try {
      await login(usuario.trim(), senha);
      onEntrou();
    } catch (falha) {
      setErro(falha instanceof Error ? falha.message : 'não consegui entrar');
    } finally {
      setEnviando(false);
    }
  };

  return (
    <div className="neo-login">
      <form className="neo-login-card" onSubmit={enviar}>
        <h1 className="neo-login-title">Neo</h1>
        <p className="neo-login-sub">Painel — entre com seu usuário</p>

        {aviso && <div className="neo-login-warn">{aviso}</div>}

        <label className="neo-login-label" htmlFor="login-usuario">Usuário</label>
        <input
          id="login-usuario"
          className="neo-login-input"
          autoComplete="username"
          autoFocus
          value={usuario}
          onChange={(e) => setUsuario(e.target.value)}
        />

        <label className="neo-login-label" htmlFor="login-senha">Senha</label>
        <input
          id="login-senha"
          className="neo-login-input"
          type="password"
          autoComplete="current-password"
          value={senha}
          onChange={(e) => setSenha(e.target.value)}
        />

        {erro && <div className="neo-login-error">{erro}</div>}

        <button
          className="neo-login-button"
          type="submit"
          disabled={enviando || !usuario.trim() || !senha}
        >
          {enviando ? 'Entrando…' : 'Entrar'}
        </button>
      </form>
    </div>
  );
}
