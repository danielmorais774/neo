export class AgentService {
  static async executeWithLoop(
    message: string,
    model: string = 'gpt-4o',
    mode: string = 'agent',
    provider?: string,
    // Ids do File Store anexados a este turno (o Neo lê por fatias; o conteúdo
    // não passa pelo chat).
    attachments?: string[],
    // Turno vindo do microfone (STT): o core tolera erro de transcrição.
    voice?: { source: 'voice'; raw?: string; engine?: string },
    // Caracteres colados do clipboard nesta mensagem — o core marca o turno como
    // contaminado acima do limiar (release-request-plan §21).
    pastedChars?: number
  ): Promise<void> {
    try {
      await window.electronAPI?.executeWithLoop(message, model, mode, provider, attachments, voice, pastedChars);
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
