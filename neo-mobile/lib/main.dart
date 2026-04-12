import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'app.dart';
import 'config/app_config.dart';
import 'providers/chat_provider.dart';
import 'providers/session_provider.dart';
import 'services/chat_service.dart';
import 'services/grpc_client.dart';
import 'services/session_service.dart';

void main() async {
  WidgetsFlutterBinding.ensureInitialized();

  SystemChrome.setSystemUIOverlayStyle(const SystemUiOverlayStyle(
    statusBarColor: Colors.transparent,
    statusBarIconBrightness: Brightness.light,
    systemNavigationBarColor: Color(0xFF0a0e1a),
    systemNavigationBarIconBrightness: Brightness.light,
  ));

  final prefs = await SharedPreferences.getInstance();
  final config = AppConfig(prefs);
  final sessionService = SessionService(prefs);
  final grpcClient = NeoCoreGrpcClient();
  final chatService = ChatService(grpcClient);

  runApp(
    MultiProvider(
      providers: [
        Provider<AppConfig>.value(value: config),
        ChangeNotifierProvider(
          create: (_) => ChatProvider(
            grpc: grpcClient,
            chatService: chatService,
            config: config,
            sessionService: sessionService,
          ),
        ),
        ChangeNotifierProvider(
          create: (_) => SessionProvider(sessionService),
        ),
      ],
      child: const NeoApp(),
    ),
  );
}
