class ConversationSession {
  final String sessionId;
  final DateTime createdAt;
  final DateTime? updatedAt;
  final String status;
  final int messageCount;

  ConversationSession({
    required this.sessionId,
    required this.createdAt,
    this.updatedAt,
    this.status = 'active',
    this.messageCount = 0,
  });
}
