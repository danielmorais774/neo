import { getCurrentWindow } from '@tauri-apps/api/window';

export class WindowService {
  static minimize(): void {
    getCurrentWindow().minimize().catch((error) => {
      console.warn('[Neo] Failed to minimize window:', error);
    });
  }

  static toggleMaximize(): void {
    getCurrentWindow().toggleMaximize().catch((error) => {
      console.warn('[Neo] Failed to toggle maximize:', error);
    });
  }

  static async startDragging(): Promise<void> {
    await getCurrentWindow().startDragging().catch((error) => {
      console.warn('[Neo] Failed to start dragging window:', error);
    });
  }

  static close(): void {
    getCurrentWindow().close().catch((error) => {
      console.warn('[Neo] Failed to close window:', error);
    });
  }

  static async getMode(): Promise<'tooltip' | 'agent'> {
    return await window.electronAPI?.getWindowMode();
  }
}
