export class WindowService {
  static minimize(): void {
    window.electronAPI?.minimizeWindow();
  }

  static close(): void {
    window.electronAPI?.closeWindow();
  }

  static async getMode(): Promise<'tooltip' | 'agent'> {
    return await window.electronAPI?.getWindowMode();
  }
}
