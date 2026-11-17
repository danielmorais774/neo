export class HistoryService {
  static async loadHistory(): Promise<any[]> {
    try {
      const history = await window.electronAPI?.getConversationHistory();
      return history || [];
    } catch (error) {
      console.error('Erro ao carregar histórico:', error);
      return [];
    }
  }

  static async clearHistory(): Promise<void> {
    try {
      await window.electronAPI?.clearHistory();
    } catch (error) {
      console.warn('Não foi possível limpar histórico via IPC:', error);
    }
  }

  static async saveHistory(messages: unknown[]): Promise<void> {
    try {
      await window.electronAPI?.saveConversationHistory(messages);
    } catch (error) {
      console.warn('Não foi possível salvar histórico via IPC:', error);
    }
  }
}
