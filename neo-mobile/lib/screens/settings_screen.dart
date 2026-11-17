import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../config/app_config.dart';
import '../config/theme.dart';
import '../providers/chat_provider.dart';
import '../providers/session_provider.dart';

class SettingsScreen extends StatefulWidget {
  const SettingsScreen({super.key});

  @override
  State<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends State<SettingsScreen> {
  late TextEditingController _hostController;
  late TextEditingController _grpcPortController;
  late TextEditingController _userIdController;
  bool _testing = false;
  String? _testResult;

  @override
  void initState() {
    super.initState();
    final config = context.read<AppConfig>();
    final session = context.read<SessionProvider>();
    _hostController = TextEditingController(text: config.host);
    _grpcPortController = TextEditingController(text: config.grpcPort.toString());
    _userIdController = TextEditingController(text: session.userId);
  }

  @override
  void dispose() {
    _hostController.dispose();
    _grpcPortController.dispose();
    _userIdController.dispose();
    super.dispose();
  }

  Future<void> _save() async {
    final config = context.read<AppConfig>();
    final session = context.read<SessionProvider>();
    await config.setHost(_hostController.text.trim());
    final port = int.tryParse(_grpcPortController.text.trim());
    if (port != null) await config.setGrpcPort(port);
    final userId = _userIdController.text.trim();
    if (userId.isNotEmpty) {
      await session.setUserId(userId);
    }

    if (mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text('Configurações salvas'),
          backgroundColor: NeoColors.success,
        ),
      );
    }
  }

  Future<void> _testConnection() async {
    final chat = context.read<ChatProvider>();
    setState(() {
      _testing = true;
      _testResult = null;
    });

    await _save();
    await chat.connect();

    if (!mounted) return;
    setState(() {
      _testing = false;
      _testResult = chat.isConnected ? 'Conectado!' : 'Falhou: ${chat.connectionError ?? "unknown"}';
    });
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Configurações')),
      body: ListView(
        padding: const EdgeInsets.all(20),
        children: [
          const Text(
            'Servidor Neo-Core',
            style: TextStyle(
              color: NeoColors.accent,
              fontSize: 16,
              fontWeight: FontWeight.w600,
            ),
          ),
          const SizedBox(height: 4),
          const Text(
            'Configure o IP e porta do servidor neo-core na sua rede',
            style: TextStyle(color: NeoColors.textSecondary, fontSize: 13),
          ),
          const SizedBox(height: 20),
          TextField(
            controller: _hostController,
            style: const TextStyle(color: NeoColors.textPrimary),
            decoration: const InputDecoration(
              labelText: 'Host / IP',
              labelStyle: TextStyle(color: NeoColors.textSecondary),
              prefixIcon: Icon(Icons.dns, color: NeoColors.textSecondary),
            ),
            keyboardType: TextInputType.url,
          ),
          const SizedBox(height: 16),
          TextField(
            controller: _grpcPortController,
            style: const TextStyle(color: NeoColors.textPrimary),
            decoration: const InputDecoration(
              labelText: 'Porta gRPC',
              labelStyle: TextStyle(color: NeoColors.textSecondary),
              prefixIcon: Icon(Icons.numbers, color: NeoColors.textSecondary),
            ),
            keyboardType: TextInputType.number,
          ),
          const SizedBox(height: 16),
          TextField(
            controller: _userIdController,
            style: const TextStyle(color: NeoColors.textPrimary),
            decoration: const InputDecoration(
              labelText: 'User ID',
              labelStyle: TextStyle(color: NeoColors.textSecondary),
              helperText: 'Use o UUID do usuário existente no back-end',
              prefixIcon: Icon(Icons.perm_identity, color: NeoColors.textSecondary),
            ),
            keyboardType: TextInputType.text,
          ),
          const SizedBox(height: 24),
          Row(
            children: [
              Expanded(
                child: OutlinedButton.icon(
                  onPressed: _testing ? null : _testConnection,
                  icon: _testing
                      ? const SizedBox(
                          width: 16,
                          height: 16,
                          child: CircularProgressIndicator(strokeWidth: 2, color: NeoColors.accent),
                        )
                      : const Icon(Icons.speed),
                  label: Text(_testing ? 'Testando...' : 'Testar conexão'),
                  style: OutlinedButton.styleFrom(
                    foregroundColor: NeoColors.accent,
                    side: const BorderSide(color: NeoColors.accent),
                    padding: const EdgeInsets.symmetric(vertical: 14),
                  ),
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: ElevatedButton.icon(
                  onPressed: _save,
                  icon: const Icon(Icons.save),
                  label: const Text('Salvar'),
                ),
              ),
            ],
          ),
          if (_testResult != null) ...[
            const SizedBox(height: 16),
            Container(
              padding: const EdgeInsets.all(12),
              decoration: BoxDecoration(
                color: _testResult!.startsWith('Conectado')
                    ? NeoColors.success.withAlpha(26)
                    : NeoColors.error.withAlpha(26),
                borderRadius: BorderRadius.circular(8),
                border: Border.all(
                  color: _testResult!.startsWith('Conectado') ? NeoColors.success : NeoColors.error,
                ),
              ),
              child: Row(
                children: [
                  Icon(
                    _testResult!.startsWith('Conectado') ? Icons.check_circle : Icons.error,
                    color: _testResult!.startsWith('Conectado') ? NeoColors.success : NeoColors.error,
                    size: 20,
                  ),
                  const SizedBox(width: 8),
                  Expanded(
                    child: Text(
                      _testResult!,
                      style: TextStyle(
                        color: _testResult!.startsWith('Conectado') ? NeoColors.success : NeoColors.error,
                        fontSize: 13,
                      ),
                    ),
                  ),
                ],
              ),
            ),
          ],
          const SizedBox(height: 32),
          const Text(
            'Sobre',
            style: TextStyle(
              color: NeoColors.accent,
              fontSize: 16,
              fontWeight: FontWeight.w600,
            ),
          ),
          const SizedBox(height: 8),
          const Text(
            'Neo Mobile v1.0.0\nChat client para neo-core via gRPC',
            style: TextStyle(color: NeoColors.textSecondary, fontSize: 13),
          ),
        ],
      ),
    );
  }
}
