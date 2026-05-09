export class AgentService {
  static async executeWithLoop(
    message: string,
    model: string = 'gpt-4o',
    mode: string = 'agent',
    provider?: string
  ): Promise<void> {
    try {
      await window.electronAPI?.executeWithLoop(message, model, mode, provider);
    } catch (error: any) {
      console.error('Erro ao executar agente:', error);
      throw new Error(error.message || 'Falha ao processar mensagem');
    }
  }

  static stopExecution(): void {
    console.log('[AgentService] Stop requested');
    window.electronAPI?.stopExecution();
  }
}
