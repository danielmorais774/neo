import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../config/theme.dart';
import '../providers/chat_provider.dart';
import '../widgets/chat/chat_input.dart';
import '../widgets/chat/chat_list.dart';
import '../widgets/chat/typing_indicator.dart';
import '../widgets/common/app_drawer.dart';
import '../widgets/common/error_banner.dart';
import 'settings_screen.dart';
import 'session_history_screen.dart';

class ChatScreen extends StatefulWidget {
  const ChatScreen({super.key});

  @override
  State<ChatScreen> createState() => _ChatScreenState();
}

class _ChatScreenState extends State<ChatScreen> {
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      context.read<ChatProvider>().connect();
    });
  }

  @override
  Widget build(BuildContext context) {
    return Consumer<ChatProvider>(
      builder: (context, chat, _) {
        return Scaffold(
          appBar: AppBar(
            title: Row(
              children: [
                Container(
                  width: 10,
                  height: 10,
                  decoration: BoxDecoration(
                    color: chat.isConnected ? NeoColors.success : NeoColors.error,
                    shape: BoxShape.circle,
                  ),
                ),
                const SizedBox(width: 10),
                const Text('Neo'),
                if (chat.currentIteration != null) ...[
                  const SizedBox(width: 10),
                  Container(
                    padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
                    decoration: BoxDecoration(
                      color: NeoColors.accent.withAlpha(31),
                      borderRadius: BorderRadius.circular(10),
                    ),
                    child: Text(
                      '${chat.currentIteration}/${chat.maxIterations ?? "?"}',
                      style: const TextStyle(color: NeoColors.accent, fontSize: 12),
                    ),
                  ),
                ],
              ],
            ),
            actions: [
              if (chat.messages.isNotEmpty)
                IconButton(
                  icon: const Icon(Icons.delete_outline),
                  tooltip: 'Limpar conversa',
                  onPressed: () => _confirmClear(context, chat),
                ),
              IconButton(
                icon: Icon(
                  chat.isConnected ? Icons.cloud_done : Icons.cloud_off,
                  color: chat.isConnected ? NeoColors.success : NeoColors.error,
                ),
                tooltip: chat.isConnected ? 'Conectado' : 'Desconectado',
                onPressed: () {
                  if (chat.isConnected) {
                    chat.disconnect();
                  } else {
                    chat.connect();
                  }
                },
              ),
            ],
          ),
          drawer: AppDrawer(
            onNewChat: () => chat.clearHistory(),
            onSettings: () => Navigator.push(
              context,
              MaterialPageRoute(builder: (_) => const SettingsScreen()),
            ),
            onSessionHistory: () => Navigator.push(
              context,
              MaterialPageRoute(builder: (_) => const SessionHistoryScreen()),
            ),
          ),
          body: Column(
            children: [
              if (chat.connectionError != null)
                ErrorBanner(
                  message: chat.connectionError!,
                  onDismiss: () => chat.connect(),
                ),
              Expanded(
                child: ChatList(messages: chat.messages),
              ),
              if (chat.isLoading) const TypingIndicator(),
              ChatInput(
                onSend: chat.sendMessage,
                enabled: chat.isConnected && !chat.isLoading,
              ),
            ],
          ),
        );
      },
    );
  }

  void _confirmClear(BuildContext context, ChatProvider chat) {
    showDialog(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: NeoColors.bgSecondary,
        title: const Text('Limpar conversa?', style: TextStyle(color: NeoColors.textPrimary)),
        content: const Text(
          'Todas as mensagens serão removidas.',
          style: TextStyle(color: NeoColors.textSecondary),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx),
            child: const Text('Cancelar'),
          ),
          TextButton(
            onPressed: () {
              chat.clearHistory();
              Navigator.pop(ctx);
            },
            child: const Text('Limpar', style: TextStyle(color: NeoColors.error)),
          ),
        ],
      ),
    );
  }
}
