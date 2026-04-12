import 'package:flutter/material.dart';

import '../config/theme.dart';

// TODO: Implement session history — fetch from /api/v1/desktop/sessions REST API

class SessionHistoryScreen extends StatelessWidget {
  const SessionHistoryScreen({super.key});

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Histórico de Sessões')),
      body: Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(Icons.history, size: 64, color: NeoColors.textSecondary.withAlpha(77)),
            const SizedBox(height: 16),
            const Text(
              'Em breve',
              style: TextStyle(color: NeoColors.textSecondary, fontSize: 16),
            ),
            const SizedBox(height: 8),
            const Text(
              'O histórico de sessões será implementado em uma atualização futura.',
              style: TextStyle(color: NeoColors.textSecondary, fontSize: 13),
              textAlign: TextAlign.center,
            ),
          ],
        ),
      ),
    );
  }
}
