// Model representing a device connected to neo-core.

class DeviceInfo {
  final String deviceId;
  final String deviceType;  // neo-desktop | neo-mobile
  final String model;
  final String osPlatform;
  final String osVersion;
  final String clientVersion;
  final String ipAddress;
  final DateTime? connectedAt;
  final bool isCurrentDevice;

  DeviceInfo({
    required this.deviceId,
    this.deviceType = 'neo-mobile',
    this.model = 'unknown',
    this.osPlatform = 'unknown',
    this.osVersion = 'unknown',
    this.clientVersion = '1.0.0',
    this.ipAddress = '',
    this.connectedAt,
    this.isCurrentDevice = false,
  });

  factory DeviceInfo.fromJson(Map<String, dynamic> json) {
    return DeviceInfo(
      deviceId: json['device_id'] ?? '',
      deviceType: json['device_type'] ?? 'unknown',
      model: json['model'] ?? 'unknown',
      osPlatform: json['os_platform'] ?? 'unknown',
      osVersion: json['os_version'] ?? 'unknown',
      clientVersion: json['client_version'] ?? '',
      ipAddress: json['ip_address'] ?? '',
      connectedAt: json['connected_at_unix'] != null
          ? DateTime.fromMillisecondsSinceEpoch(json['connected_at_unix'] * 1000)
          : null,
      isCurrentDevice: json['is_current_device'] ?? false,
    );
  }

  Map<String, dynamic> toJson() {
    return {
      'device_id': deviceId,
      'device_type': deviceType,
      'model': model,
      'os_platform': osPlatform,
      'os_version': osVersion,
      'client_version': clientVersion,
      'ip_address': ipAddress,
      'connected_at_unix': connectedAt?.millisecondsSinceEpoch,
      'is_current_device': isCurrentDevice,
    };
  }

  @override
  String toString() {
    return 'DeviceInfo(id: $deviceId, type: $deviceType, platform: $osPlatform)';
  }
}
