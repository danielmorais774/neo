import { useState, useRef, useEffect } from 'react';
import './PasswordModal.css';

interface PasswordModalProps {
  prompt: string;
  onSubmit: (password: string) => void;
  onCancel: () => void;
}

function PasswordModal({ prompt, onSubmit, onCancel }: PasswordModalProps) {
  const [password, setPassword] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    // Auto-focus the password input
    inputRef.current?.focus();
  }, []);

  const handleSubmit = () => {
    if (password) {
      onSubmit(password);
      setPassword('');
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && password) {
      handleSubmit();
    } else if (e.key === 'Escape') {
      onCancel();
    }
  };

  return (
    <div className="password-overlay">
      <div className="password-modal">
        <div className="password-header">
          <span className="password-icon">🔑</span>
          <h3>Senha Necessária</h3>
        </div>

        <div className="password-content">
          <div className="password-prompt-text">
            {prompt}
          </div>

          <div className="password-input-wrapper">
            <input
              ref={inputRef}
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Digite sua senha..."
              autoComplete="off"
            />
          </div>

          <div className="password-warning">
            <span className="password-warning-icon">🔒</span>
            <span>A senha será enviada diretamente ao comando em execução. O Neo não armazena sua senha.</span>
          </div>
        </div>

        <div className="password-actions">
          <button className="password-btn-cancel" onClick={onCancel}>
            <span>✕</span> Cancelar
          </button>
          <button className="password-btn-send" onClick={handleSubmit} disabled={!password}>
            <span>↵</span> Enviar
          </button>
        </div>
      </div>
    </div>
  );
}

export default PasswordModal;
