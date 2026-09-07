export class CommandService {
  static async executeCommand(command: string): Promise<{ success: boolean; output?: string; error?: string }> {
    try {
      return await window.electronAPI?.executeCommand(command);
    } catch (error: any) {
      return {
        success: false,
        error: error.message || 'Erro ao executar comando'
      };
    }
  }

  static respondToPermission(requestId: string, approved: boolean): void {
    const api = window.electronAPI;
    if (!api?.respondToPermission) {
      // Sem ponte não há como aprovar: grita em vez de virar no-op silencioso — o
      // sintoma seria o mesmo botão travado em "Aguardando...".
      console.error('[CommandService] electronAPI.respondToPermission indisponível', { requestId, approved });
      return;
    }
    api.respondToPermission(requestId, approved);
  }
}
