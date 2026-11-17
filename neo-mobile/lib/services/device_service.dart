// Device service for neo-mobile.
// Handles device registration and listing connected devices.

import '../models/device_info.dart';
import '../utils/logger.dart';

class DeviceService {
  static const _tag = 'DeviceService';
  
  final String _host;
  final int _httpPort;

  DeviceService({required String host, int httpPort = 8000})
      : _host = host,
        _httpPort = httpPort;

  /// List all devices connected to neo-core via REST API.
  Future<List<DeviceInfo>> listConnectedDevices() async {
    try {
      final uri = Uri.http('$_host:$_httpPort', '/api/v1/devices');
      final response = await _fetch(uri);
      
      if (response['devices'] != null) {
        final List<dynamic> devicesJson = response['devices'];
        return devicesJson
            .map((json) => DeviceInfo.fromJson(json as Map<String, dynamic>))
            .toList();
      }
      return [];
    } catch (e) {
      Log.e(_tag, 'Failed to list devices', e);
      return [];
    }
  }

  Map<String, dynamic> _fetch(Uri uri) {
    // Using sync HTTP for simplicity — in production use http package
    throw UnimplementedError('Use http package for actual HTTP requests');
  }
}
