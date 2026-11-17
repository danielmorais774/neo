const String kAppName = 'Neo Mobile';
const String kServiceName = 'neo-mobile';
const String kDefaultHost = '192.168.1.100';
const int kDefaultGrpcPort = 50051;
const int kDefaultHttpPort = 8000;
const int kMaxReconnectAttempts = 5;
const Duration kReconnectDelay = Duration(seconds: 3);
const Duration kKeepAliveInterval = Duration(seconds: 30);
