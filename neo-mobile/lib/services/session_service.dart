import 'package:shared_preferences/shared_preferences.dart';
import 'package:uuid/uuid.dart';

class SessionService {
  static const _keyActiveSession = 'active_session_id';
  static const _keyUserId = 'user_id';
  final SharedPreferences _prefs;

  SessionService(this._prefs);

  String? get activeSessionId => _prefs.getString(_keyActiveSession);

  Future<void> setActiveSession(String sessionId) =>
      _prefs.setString(_keyActiveSession, sessionId);

  Future<void> clearActiveSession() => _prefs.remove(_keyActiveSession);

  /// Get or set the persistent user UUID.
  /// Creates one on first run and stores it.
  String get userId {
    final existing = _prefs.getString(_keyUserId);
    if (existing != null && existing.isNotEmpty) return existing;
    final newId = const Uuid().v4();
    _prefs.setString(_keyUserId, newId);
    return newId;
  }

  Future<void> setUserId(String userId) async {
    await _prefs.setString(_keyUserId, userId);
  }
}
