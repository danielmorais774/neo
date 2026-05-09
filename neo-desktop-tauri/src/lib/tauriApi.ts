// Tauri API wrapper - replaces Electron IPC with Tauri invoke/listen

import { invoke } from '@tauri-apps/api/core';
import { listen, type UnlistenFn } from '@tauri-apps/api/event';
import { emit } from '@tauri-apps/api/event';

// Types matching the original ElectronAPI interface
export interface AgentEvent {
  type: string;
  data: any;
  timestamp?: string;
}

export interface DeviceInfo {
  device_id: string;
  device_type: string;
  model: string;
  os_platform: string;
  os_version: string;
  client_version: string;
  ip_address: string;
  connected_at_unix: number;
  is_current_device: boolean;
}

export interface NeoNotification {
  id: string;
  userId: string;
  missionId: string | null;
  kind: string;
  title: string;
  body: string;
  payload: Record<string, unknown>;
  createdAtUnix: number;
}

export interface ElectronAPI {
  sendMessage: (message: string, model?: string, mode?: string) => Promise<unknown>;
  executeWithLoop: (message: string, model?: string, mode?: string, provider?: string) => Promise<void>;
  stopExecution: () => void;
  resetAgent: (newSessionId: string) => Promise<string>;
  onAgentEvent: (callback: (event: AgentEvent) => void) => UnlistenFn;
  executeCommand: (command: string) => Promise<{ success: boolean; output?: string; error?: string }>;
  onCommandRequest: (callback: (command: string, explanation: string) => void) => UnlistenFn;
  respondToPermission: (requestId: string, approved: boolean) => void;
  onPasswordRequest: (callback: (prompt: string) => void) => UnlistenFn;
  respondToPassword: (password: string | null) => void;
  getConversationHistory: () => Promise<unknown[]>;
  clearHistory: () => Promise<void>;
  getWindowMode: () => Promise<'tooltip' | 'agent'>;
  getNeoCoreStatus: () => Promise<{
    connected: boolean;
    status: string;
    service?: string;
    checkedAt: string;
  }>;
  onAppStarted: (callback: (payload: unknown) => void) => UnlistenFn;
  minimizeWindow: () => void;
  closeWindow: () => void;
  onScreenCapture: (callback: (data: { imageDataUrl: string; question: string }) => void) => UnlistenFn;
  quickHelpAskStream: (question: string) => void;
  onQuickHelpChunk: (cb: (chunk: string) => void) => UnlistenFn;
  onQuickHelpDone: (cb: () => void) => UnlistenFn;
  onQuickHelpError: (cb: (msg: string) => void) => UnlistenFn;
  quickHelpResize: (expanded: boolean) => void;
  quickHelpMoveBy: (dx: number, dy: number) => void;
  onNotification: (callback: (notification: NeoNotification) => void) => UnlistenFn;
  ackNotification: (notificationId: string) => Promise<{ ok: boolean; error?: string }>;
  listConnectedDevices: () => Promise<DeviceInfo[]>;
}

let passwordRequestUnlisten: UnlistenFn | null = null;
let appStartedUnlisten: UnlistenFn | null = null;
let screenCaptureUnlisten: UnlistenFn | null = null;
let quickHelpChunkUnlisten: UnlistenFn | null = null;
let quickHelpDoneUnlisten: UnlistenFn | null = null;
let quickHelpErrorUnlisten: UnlistenFn | null = null;
let notificationUnlisten: UnlistenFn | null = null;

export const electronAPI: ElectronAPI = {
  sendMessage: (message: string, model?: string, mode?: string) =>
    invoke('execute_with_loop', { message, model, mode, provider: undefined }),

  executeWithLoop: (message: string, model?: string, mode?: string, provider?: string) =>
    invoke('execute_with_loop', { message, model, mode, provider }),

  stopExecution: () => invoke('stop_execution'),

  resetAgent: (newSessionId: string) => invoke('reset_agent', { newSessionId }),

  onAgentEvent: (callback: (event: AgentEvent) => void) => {
    // Each call owns its own unlisten so multiple subscribers (or React
    // StrictMode double-mount) don't leak listeners onto a shared global.
    let cancelled = false;
    let unlisten: UnlistenFn | null = null;
    listen<AgentEvent>('agent-event', (event) => callback(event.payload)).then((u) => {
      if (cancelled) {
        u();
      } else {
        unlisten = u;
      }
    });
    return () => {
      cancelled = true;
      if (unlisten) {
        unlisten();
        unlisten = null;
      }
    };
  },

  executeCommand: (command: string) => invoke('execute_command', { command }),

  onCommandRequest: (callback: (command: string, explanation: string) => void) => {
    let cancelled = false;
    let unlisten: UnlistenFn | null = null;
    listen<{ cmd: string; explanation: string }>('command-request', (event) => {
      callback(event.payload.cmd, event.payload.explanation);
    }).then((u) => {
      if (cancelled) {
        u();
      } else {
        unlisten = u;
      }
    });
    return () => {
      cancelled = true;
      if (unlisten) {
        unlisten();
        unlisten = null;
      }
    };
  },

  respondToPermission: (_requestId: string, _approved: boolean) => {
    // In Tauri this would be handled differently
  },

  onPasswordRequest: (callback: (prompt: string) => void) => {
    if (passwordRequestUnlisten) passwordRequestUnlisten();
    listen<string>('password-request', (event) => {
      callback(event.payload);
    }).then((unlisten) => {
      passwordRequestUnlisten = unlisten;
    });
    return () => {
      if (passwordRequestUnlisten) {
        passwordRequestUnlisten();
        passwordRequestUnlisten = null;
      }
    };
  },

  respondToPassword: (_password: string | null) => {
    // In Tauri this would be handled differently
  },

  getConversationHistory: () => invoke('get_conversation_history'),
  clearHistory: () => invoke('clear_conversation_history'),

  getWindowMode: () => invoke('get_window_mode'),

  getNeoCoreStatus: () => invoke('get_neo_core_status'),

  onAppStarted: (callback: (payload: unknown) => void) => {
    if (appStartedUnlisten) appStartedUnlisten();
    listen<unknown>('app-started', (event) => callback(event.payload)).then((unlisten) => {
      appStartedUnlisten = unlisten;
    });
    return () => {
      if (appStartedUnlisten) {
        appStartedUnlisten();
        appStartedUnlisten = null;
      }
    };
  },

  minimizeWindow: () => {
    emit('minimize-window');
  },

  closeWindow: () => {
    emit('close-window');
  },

  onScreenCapture: (callback: (data: { imageDataUrl: string; question: string }) => void) => {
    if (screenCaptureUnlisten) screenCaptureUnlisten();
    listen<{ imageDataUrl: string; question: string }>('screen-capture', (event) => {
      callback(event.payload);
    }).then((unlisten) => {
      screenCaptureUnlisten = unlisten;
    });
    return () => {
      if (screenCaptureUnlisten) {
        screenCaptureUnlisten();
        screenCaptureUnlisten = null;
      }
    };
  },

  quickHelpAskStream: (question: string) => {
    invoke('quick_help_ask_stream', { question }).catch(console.error);
  },

  onQuickHelpChunk: (cb: (chunk: string) => void) => {
    if (quickHelpChunkUnlisten) quickHelpChunkUnlisten();
    listen<string>('quick-help-chunk', (event) => {
      cb(event.payload);
    }).then((unlisten) => {
      quickHelpChunkUnlisten = unlisten;
    });
    return () => {
      if (quickHelpChunkUnlisten) {
        quickHelpChunkUnlisten();
        quickHelpChunkUnlisten = null;
      }
    };
  },

  onQuickHelpDone: (cb: () => void) => {
    if (quickHelpDoneUnlisten) quickHelpDoneUnlisten();
    listen('quick-help-done', () => cb()).then((unlisten) => {
      quickHelpDoneUnlisten = unlisten;
    });
    return () => {
      if (quickHelpDoneUnlisten) {
        quickHelpDoneUnlisten();
        quickHelpDoneUnlisten = null;
      }
    };
  },

  onQuickHelpError: (cb: (msg: string) => void) => {
    if (quickHelpErrorUnlisten) quickHelpErrorUnlisten();
    listen<string>('quick-help-error', (event) => {
      cb(event.payload);
    }).then((unlisten) => {
      quickHelpErrorUnlisten = unlisten;
    });
    return () => {
      if (quickHelpErrorUnlisten) {
        quickHelpErrorUnlisten();
        quickHelpErrorUnlisten = null;
      }
    };
  },

  quickHelpResize: (expanded: boolean) => {
    emit('quick-help-resize', expanded);
  },

  quickHelpMoveBy: (dx: number, dy: number) => {
    emit('quick-help-move-by', { dx, dy });
  },

  onNotification: (callback: (notification: NeoNotification) => void) => {
    if (notificationUnlisten) notificationUnlisten();
    listen<NeoNotification>('notification', (event) => {
      callback(event.payload);
    }).then((unlisten) => {
      notificationUnlisten = unlisten;
    });
    return () => {
      if (notificationUnlisten) {
        notificationUnlisten();
        notificationUnlisten = null;
      }
    };
  },

  ackNotification: (notificationId: string) => invoke('ack_notification', { notificationId }),

  listConnectedDevices: () => invoke('list_connected_devices'),
};