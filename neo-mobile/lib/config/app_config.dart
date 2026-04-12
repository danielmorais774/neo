import 'package:shared_preferences/shared_preferences.dart';
import '../utils/constants.dart';

class AppConfig {
  static const _keyHost = 'grpc_host';
  static const _keyGrpcPort = 'grpc_port';
  static const _keyHttpPort = 'http_port';

  final SharedPreferences _prefs;

  AppConfig(this._prefs);

  String get host => _prefs.getString(_keyHost) ?? kDefaultHost;
  int get grpcPort => _prefs.getInt(_keyGrpcPort) ?? kDefaultGrpcPort;
  int get httpPort => _prefs.getInt(_keyHttpPort) ?? kDefaultHttpPort;

  Future<void> setHost(String value) => _prefs.setString(_keyHost, value);
  Future<void> setGrpcPort(int value) => _prefs.setInt(_keyGrpcPort, value);
  Future<void> setHttpPort(int value) => _prefs.setInt(_keyHttpPort, value);
}
