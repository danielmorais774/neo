import 'package:flutter/material.dart';

import '../../config/theme.dart';

class AppDrawer extends StatelessWidget {
  final VoidCallback onNewChat;
  final VoidCallback onSettings;
  final VoidCallback onSessionHistory;

  const AppDrawer({
    super.key,
    required this.onNewChat,
    required this.onSettings,
    required this.onSessionHistory,
  });

  @override
  Widget build(BuildContext context) {
    return Drawer(
      backgroundColor: NeoColors.bgSecondary,
      child: SafeArea(
        child: Column(
          children: [
            Container(
              padding: const EdgeInsets.all(20),
              width: double.infinity,
              decoration: const BoxDecoration(
                border: Border(bottom: BorderSide(color: NeoColors.borderColor)),
              ),
              child: const Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    'Neo',
                    style: TextStyle(
                      color: NeoColors.accent,
                      fontSize: 28,
                      fontWeight: FontWeight.bold,
                    ),
                  ),
                  SizedBox(height: 4),
                  Text(
                    'Mobile Assistant',
                    style: TextStyle(color: NeoColors.textSecondary, fontSize: 14),
                  ),
                ],
              ),
            ),
            const SizedBox(height: 8),
            _DrawerItem(
              icon: Icons.add_comment,
              label: 'Nova conversa',
              onTap: () {
                Navigator.pop(context);
                onNewChat();
              },
            ),
            _DrawerItem(
              icon: Icons.history,
              label: 'Histórico',
              onTap: () {
                Navigator.pop(context);
                onSessionHistory();
              },
            ),
            const Spacer(),
            const Divider(color: NeoColors.borderColor),
            _DrawerItem(
              icon: Icons.settings,
              label: 'Configurações',
              onTap: () {
                Navigator.pop(context);
                onSettings();
              },
            ),
            const SizedBox(height: 8),
          ],
        ),
      ),
    );
  }
}

class _DrawerItem extends StatelessWidget {
  final IconData icon;
  final String label;
  final VoidCallback onTap;

  const _DrawerItem({
    required this.icon,
    required this.label,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return ListTile(
      leading: Icon(icon, color: NeoColors.textSecondary),
      title: Text(label, style: const TextStyle(color: NeoColors.textPrimary)),
      onTap: onTap,
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8)),
      contentPadding: const EdgeInsets.symmetric(horizontal: 20),
    );
  }
}
