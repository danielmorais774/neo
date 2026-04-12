import 'dart:async';

import 'package:uuid/uuid.dart';

import '../models/chat_message.dart';
import '../utils/logger.dart';
import 'grpc_client.dart';

class ChatService {
  static const _tag = 'ChatService';
  final NeoCoreGrpcClient _grpc;
  String? _sessionId;

  String? get sessionId => _sessionId;

  // Stream controllers for UI consumption
  final _messageController = StreamController<ChatMessage>.broadcast();
  final _statusController = StreamController<String>.broadcast();

  Stream<ChatMessage> get messageStream => _messageController.stream;
  Stream<String> get statusStream => _statusController.stream;

  ChatService(this._grpc) {
    _grpc.onThinking = _onThinking;
    _grpc.onResponse = _onResponse;
    _grpc.onToolRequest = _onToolRequest;
    _grpc.onCommandRequest = _onCommandRequest;
    _grpc.onError = _onError;
    _grpc.onIteration = _onIteration;
    _grpc.onStreamEnd = _onStreamEnd;
  }

  void sendMessage(String text, {String? userId}) {
    _sessionId ??= const Uuid().v4();
    Log.i(_tag, 'Sending message (session: $_sessionId, user: ${userId ?? 'anon'})');
    _grpc.executeTask(_sessionId!, text, userId: userId);
  }

  void clearSession() {
    _sessionId = null;
    Log.i(_tag, 'Session cleared');
  }

  void _onThinking(String message, int iteration) {
    _messageController.add(ChatMessage(
      role: MessageRole.thinking,
      content: message,
      isFinal: false,
      iteration: iteration,
    ));
  }

  void _onResponse(String message, bool isFinal, int totalIterations) {
    _messageController.add(ChatMessage(
      role: MessageRole.assistant,
      content: message,
      isFinal: isFinal,
      iteration: totalIterations,
    ));
    if (isFinal) {
      if (_grpc.activeSessionId != null) {
        _sessionId = _grpc.activeSessionId;
      }
      _statusController.add('idle');
    }
  }

  void _onToolRequest(String toolCallId, String toolName, String arguments, bool requiresApproval) {
    // On mobile, we auto-decline tool requests (no device tools implemented yet)
    // But we show the request in chat for visibility
    _messageController.add(ChatMessage(
      role: MessageRole.system,
      content: '🔧 Tool requested: $toolName (auto-declined on mobile)',
      toolRequest: {
        'tool_call_id': toolCallId,
        'tool_name': toolName,
        'arguments': arguments,
        'requires_approval': requiresApproval,
      },
    ));

    // Auto-decline: send back a non-success result
    _grpc.sendToolResult(
      toolCallId: toolCallId,
      toolName: toolName,
      success: false,
      result: '',
      error: 'Tool execution not available on neo-mobile. Only chat is supported.',
    );
  }

  void _onCommandRequest(String commandId, String command, String explanation, bool isBackground) {
    _messageController.add(ChatMessage(
      role: MessageRole.system,
      content: '⚡ Command requested: $command\n$explanation\n(auto-declined on mobile)',
      commandRequest: {
        'command_id': commandId,
        'command': command,
        'explanation': explanation,
        'is_background': isBackground,
      },
    ));

    // Auto-decline commands on mobile
    _grpc.sendCommandApproval(commandId: commandId, approved: false);
  }

  void _onError(String error, String details) {
    _messageController.add(ChatMessage(
      role: MessageRole.error,
      content: details.isNotEmpty ? '$error\n$details' : error,
    ));
    _statusController.add('error');
  }

  void _onIteration(int current, int max) {
    _statusController.add('iteration:$current/$max');
  }

  void _onStreamEnd() {
    _statusController.add('idle');
  }

  void dispose() {
    _messageController.close();
    _statusController.close();
  }
}
