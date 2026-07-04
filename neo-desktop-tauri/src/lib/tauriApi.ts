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
  hardware_id: string;
  os_platform: string;
  os_version: string;
  client_version: string;
  ip_address: string;
  connected_at_unix: number;
  is_current_device: boolean;
}

export interface NotificationAction {
  action_type: string;
  label: string;
  payload?: Record<string, unknown>;
}

export interface NotificationField {
  label: string;
  value: string;
}

export interface NotificationConfig {
  duration_ms?: number;
  position?: 'top_left' | 'top_right' | 'bottom_left' | 'bottom_right' | 'center';
  persistent?: boolean;
  play_sound?: boolean;
}

export interface NeoNotification {
  id: string;
  userId: string;
  missionId: string | null;
  kind: string;
  title: string;
  description: string;
  fields?: NotificationField[];
  actions?: NotificationAction[];
  payload: Record<string, unknown>;
  createdAtUnix: number;
  config?: NotificationConfig;
}

export interface NotificationActionEvent {
  notification_id: string;
  action_type: string;
  action_payload?: Record<string, unknown>;
}

export interface VoiceConfig {
  wsBaseUrl: string;
  apiKey: string | null;
  voice: string | null;
  backend: string;
  language: string | null;
}

// Engine de voz (STT/TTS) controlável no PC gamer via os scripts engine_ctl.
export interface VoiceEngine {
  key: string;
  label: string;
  kind: string; // "Windows" | "WSL2"
  port: number;
  backend: string | null; // id do backend no neo-voice (null p/ o STT)
}

export type VoiceEngineAction = 'start' | 'stop' | 'restart' | 'status';

// Estado do detector de wake word local (Estágio 1 do Modo Live).
export interface WakeStatus {
  running: boolean;
  python: string;
  script: string;
  refsDir: string;
  threshold: number;
  cooldownMs: number;
}

// Evento `wake-detected`: o sidecar reconheceu a palavra-gatilho.
export interface WakeDetected {
  model: string;
  score: number;
}

// Frase-gatilho cadastrada (few-shot pela voz do usuário).
export interface WakePhrase {
  slug: string;
  label: string;
  sampleCount: number;
  enrolled: boolean; // referência (embeddings) já gerada
  minSamples: number; // mínimo exigido p/ gerar a referência
  role: 'wake' | 'stop'; // "wake" acende o Live; "stop" ("pare") interrompe
}

// Active voice resolved by neo-core (from the DB) and carried on the agent's
// final reply (Pattern A). When present it overrides the static VoiceConfig for
// that utterance. `engine` maps to the neo-voice `backend` field.
export interface VoiceOverride {
  engine?: string | null;
  voice?: string | null;
  language?: string | null;
  label?: string | null;
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
  saveConversationHistory: (messages: unknown[]) => Promise<void>;
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
  onNotificationAction: (callback: (event: NotificationActionEvent) => void) => UnlistenFn;
  listConnectedDevices: () => Promise<DeviceInfo[]>;
  setNotificationContext: (activeTab: 'chat' | 'devices') => Promise<void>;
  getVoiceConfig: () => Promise<VoiceConfig>;
  // Motores de voz no PC gamer (subir/baixar via engine_ctl por SSH).
  listVoiceEngines: () => Promise<VoiceEngine[]>;
  voiceEngineHealth: (key: string) => Promise<boolean>;
  voiceEngineAction: (key: string, action: VoiceEngineAction) => Promise<string>;
  // Wake word local (Estágio 1 do Modo Live).
  wakeStart: () => Promise<WakeStatus>;
  wakeStop: () => Promise<WakeStatus>;
  wakeStatus: () => Promise<WakeStatus>;
  onWakeDetected: (callback: (detected: WakeDetected) => void) => UnlistenFn;
  // Palavra "pare" (papel "stop"): interrompe a fala/o turno no Modo Live.
  onWakeInterrupt: (callback: (detected: WakeDetected) => void) => UnlistenFn;
  // Cadastro de frases-gatilho (few-shot pela voz).
  wakePhrases: () => Promise<WakePhrase[]>;
  wakeAddSample: (label: string, samples: number[], role?: 'wake' | 'stop') => Promise<WakePhrase>;
  wakeDeletePhrase: (slug: string) => Promise<void>;
  wakeEnroll: (slug: string) => Promise<WakePhrase>;
}

let passwordRequestUnlisten: UnlistenFn | null = null;
let appStartedUnlisten: UnlistenFn | null = null;
let screenCaptureUnlisten: UnlistenFn | null = null;
let quickHelpChunkUnlisten: UnlistenFn | null = null;
let quickHelpDoneUnlisten: UnlistenFn | null = null;
let quickHelpErrorUnlisten: UnlistenFn | null = null;

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

  respondToPermission: (requestId: string, approved: boolean) => {
    // Push the approval/rejection into the live agent stream. Without this the
    // run hangs at "Aguardando permissão" until the 300s timeout.
    invoke('respond_to_command', { commandId: requestId, approved }).catch((e) => {
      console.error('[tauriApi] respond_to_command failed:', e);
    });
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
  saveConversationHistory: (messages: unknown[]) =>
    invoke('save_conversation_history', { messages }),

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
    let cancelled = false;
    let unlisten: UnlistenFn | null = null;
    listen<NeoNotification>('notification', (event) => {
      callback(event.payload);
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

  ackNotification: (notificationId: string) => invoke('ack_notification', { notificationId }),

  onNotificationAction: (callback: (event: NotificationActionEvent) => void) => {
    let cancelled = false;
    let unlisten: UnlistenFn | null = null;
    listen<NotificationActionEvent>('notification-action', (event) => {
      callback(event.payload);
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

  listConnectedDevices: () => invoke('list_connected_devices'),
  setNotificationContext: (activeTab: 'chat' | 'devices') =>
    invoke('set_notification_context', { activeTab }),

  getVoiceConfig: () => invoke('get_voice_config'),

  listVoiceEngines: () => invoke('list_voice_engines'),
  voiceEngineHealth: (key: string) => invoke('voice_engine_health', { key }),
  voiceEngineAction: (key: string, action: VoiceEngineAction) =>
    invoke('voice_engine_action', { key, action }),

  wakeStart: () => invoke('wake_start'),
  wakeStop: () => invoke('wake_stop'),
  wakeStatus: () => invoke('wake_status'),

  onWakeDetected: (callback: (detected: WakeDetected) => void) => {
    // Cada assinante tem seu próprio unlisten (igual onAgentEvent) p/ não vazar
    // listeners no StrictMode/double-mount.
    let cancelled = false;
    let unlisten: UnlistenFn | null = null;
    listen<WakeDetected>('wake-detected', (event) => callback(event.payload)).then((u) => {
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

  onWakeInterrupt: (callback: (detected: WakeDetected) => void) => {
    let cancelled = false;
    let unlisten: UnlistenFn | null = null;
    listen<WakeDetected>('wake-interrupt', (event) => callback(event.payload)).then((u) => {
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

  wakePhrases: () => invoke('wake_phrases'),
  wakeAddSample: (label: string, samples: number[], role: 'wake' | 'stop' = 'wake') =>
    invoke('wake_add_sample', { label, samples, role }),
  wakeDeletePhrase: (slug: string) => invoke('wake_delete_phrase', { slug }),
  wakeEnroll: (slug: string) => invoke('wake_enroll', { slug }),
};
