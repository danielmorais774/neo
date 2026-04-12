import 'package:flutter/foundation.dart';

import '../models/conversation_session.dart';
import '../services/session_service.dart';

class SessionProvider extends ChangeNotifier {
  final SessionService _sessionService;
  final List<ConversationSession> _sessions = [];

  List<ConversationSession> get sessions => List.unmodifiable(_sessions);
  String? get activeSessionId => _sessionService.activeSessionId;
  String get userId => _sessionService.userId;

  SessionProvider(this._sessionService);

  Future<void> setActiveSession(String sessionId) async {
    await _sessionService.setActiveSession(sessionId);
    notifyListeners();
  }

  Future<void> setUserId(String userId) async {
    await _sessionService.setUserId(userId);
    notifyListeners();
  }

  Future<void> clearActiveSession() async {
    await _sessionService.clearActiveSession();
    notifyListeners();
  }

  // TODO: Fetch session list from REST API /api/v1/desktop/sessions
}
