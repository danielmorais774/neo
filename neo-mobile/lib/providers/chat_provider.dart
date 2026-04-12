import 'dart:async';

import 'package:flutter/foundation.dart';

import '../config/app_config.dart';
import '../models/chat_message.dart';
import '../services/chat_service.dart';
import '../services/grpc_client.dart';
import '../services/session_service.dart';
import '../utils/logger.dart';

class ChatProvider extends ChangeNotifier {
  static const _tag = 'ChatProvider';

  final NeoCoreGrpcClient _grpc;
  final ChatService _chatService;
  final AppConfig _config;
  final SessionService _sessionService;

  final List<ChatMessage> _messages = [];
  bool _isLoading = false;
  bool _isConnected = false;
  int? _currentIteration;
  int? _maxIterations;
  String? _connectionError;

  List<ChatMessage> get messages => List.unmodifiable(_messages);
  bool get isLoading => _isLoading;
  bool get isConnected => _isConnected;
  int? get currentIteration => _currentIteration;
  int? get maxIterations => _maxIterations;
  String? get connectionError => _connectionError;
  String? get sessionId => _chatService.sessionId;

  StreamSubscription<ChatMessage>? _messageSub;
  StreamSubscription<String>? _statusSub;

  ChatProvider({
    required NeoCoreGrpcClient grpc,
    required ChatService chatService,
    required AppConfig config,
    required SessionService sessionService,
  })  : _grpc = grpc,
        _chatService = chatService,
        _config = config,
        _sessionService = sessionService {
    _messageSub = _chatService.messageStream.listen(_onMessage);
    _statusSub = _chatService.statusStream.listen(_onStatus);
  }

  Future<void> connect() async {
    try {
      _connectionError = null;
      await _grpc.connect(_config.host, _config.grpcPort);
      final healthy = await _grpc.healthCheck();
      _isConnected = healthy;
      if (!healthy) {
        _connectionError = 'Server is not healthy';
      }
    } catch (e) {
      _isConnected = false;
      _connectionError = e.toString();
      Log.e(_tag, 'Connect failed', e);
    }
    notifyListeners();
  }

  Future<void> disconnect() async {
    await _grpc.disconnect();
    _isConnected = false;
    _connectionError = null;
    notifyListeners();
  }

  void sendMessage(String text) {
    if (text.trim().isEmpty) return;

    _messages.add(ChatMessage(role: MessageRole.user, content: text));
    _isLoading = true;
    _currentIteration = null;
    _maxIterations = null;
    notifyListeners();

    _chatService.sendMessage(text, userId: _sessionService.userId);
  }

  void clearHistory() {
    _messages.clear();
    _chatService.clearSession();
    _currentIteration = null;
    _maxIterations = null;
    notifyListeners();
  }

  void _onMessage(ChatMessage msg) {
    if (msg.isThinking) {
      // Replace previous thinking message or add new
      final idx = _messages.lastIndexWhere((m) => m.isThinking);
      if (idx >= 0) {
        _messages[idx] = msg;
      } else {
        _messages.add(msg);
      }
    } else if (msg.isAssistant && msg.isFinal) {
      // Remove thinking messages, add final response
      _messages.removeWhere((m) => m.isThinking);
      _messages.add(msg);
      _isLoading = false;
    } else {
      _messages.add(msg);
    }
    notifyListeners();
  }

  void _onStatus(String status) {
    if (status == 'idle') {
      _isLoading = false;
      _currentIteration = null;
      _maxIterations = null;
    } else if (status == 'error') {
      _isLoading = false;
    } else if (status.startsWith('iteration:')) {
      final parts = status.split(':')[1].split('/');
      _currentIteration = int.tryParse(parts[0]);
      _maxIterations = int.tryParse(parts[1]);
    }
    notifyListeners();
  }

  @override
  void dispose() {
    _messageSub?.cancel();
    _statusSub?.cancel();
    _chatService.dispose();
    _grpc.disconnect();
    super.dispose();
  }
}
