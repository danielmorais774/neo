import './PermissionModal.css';

interface PermissionModalProps {
  command: string;
  explanation: string;
  onApprove: () => void;
  onReject: () => void;
}

function PermissionModal({ command, explanation, onApprove, onReject }: PermissionModalProps) {
  return (
    <div className="permission-overlay">
      <div className="permission-modal">
        <div className="permission-header">
          <span className="permission-icon">🔐</span>
          <h3>Permissão Necessária</h3>
        </div>
        
        <div className="permission-content">
          <p className="permission-explanation">{explanation}</p>
          
          <div className="permission-command">
            <div className="command-label">Comando a ser executado:</div>
            <code>{command}</code>
          </div>

          <div className="permission-warning">
            <span className="warning-icon">⚠️</span>
            <span>Este comando será executado no seu terminal. Revise antes de aprovar.</span>
          </div>
        </div>

        <div className="permission-actions">
          <button className="btn-reject" onClick={onReject}>
            <span>✕</span> Rejeitar
          </button>
          <button className="btn-approve" onClick={onApprove}>
            <span>✓</span> Aprovar e Executar
          </button>
        </div>
      </div>
    </div>
  );
}

export default PermissionModal;
