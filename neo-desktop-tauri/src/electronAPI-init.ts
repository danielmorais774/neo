// Expose electronAPI to window for backward compatibility with existing code
import { electronAPI } from './lib/tauriApi';

// This makes electronAPI available as window.electronAPI
// so existing code using window.electronAPI?.method() continues to work
if (typeof window !== 'undefined') {
  (window as any).electronAPI = electronAPI;
  console.log('[Neo] electronAPI exposed to window');
}
