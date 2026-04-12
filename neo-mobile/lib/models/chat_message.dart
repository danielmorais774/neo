import 'package:uuid/uuid.dart';

enum MessageRole { user, assistant, thinking, system, error }

class ChatMessage {
  final String id;
  final MessageRole role;
  final String content;
  final DateTime timestamp;
  final bool isFinal;
  final int? iteration;
  final Map<String, dynamic>? toolRequest;
  final Map<String, dynamic>? commandRequest;

  ChatMessage({
    String? id,
    required this.role,
    required this.content,
    DateTime? timestamp,
    this.isFinal = true,
    this.iteration,
    this.toolRequest,
    this.commandRequest,
  })  : id = id ?? const Uuid().v4(),
        timestamp = timestamp ?? DateTime.now();

  bool get isThinking => role == MessageRole.thinking;
  bool get isUser => role == MessageRole.user;
  bool get isAssistant => role == MessageRole.assistant;
  bool get isError => role == MessageRole.error;
  bool get hasToolRequest => toolRequest != null;
  bool get hasCommandRequest => commandRequest != null;
}
