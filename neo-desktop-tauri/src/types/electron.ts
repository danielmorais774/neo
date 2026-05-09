// Re-export Tauri API types (replaces Electron IPC types)
export { electronAPI } from '../lib/tauriApi';
export type { ElectronAPI, DeviceInfo, NeoNotification } from '../lib/tauriApi';

export type WindowMode = 'tooltip' | 'agent';
export type ChatMode = 'agent' | 'plan' | 'ask';

import type { ElectronAPI } from '../lib/tauriApi';
declare global {
  interface Window {
    electronAPI: ElectronAPI;
  }
}