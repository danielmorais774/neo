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
    window.electronAPI?.respondToPermission(requestId, approved);
  }
}
