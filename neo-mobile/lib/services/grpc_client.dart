import 'dart:async';
import 'dart:io';

import 'package:fixnum/fixnum.dart';
import 'package:grpc/grpc.dart';

import '../generated/neo_desktop.pbgrpc.dart';
import '../utils/constants.dart';
import '../utils/logger.dart';

typedef OnThinking = void Function(String message, int iteration);
typedef OnResponse = void Function(String message, bool isFinal, int totalIterations);
typedef OnToolRequest = void Function(String toolCallId, String toolName, String arguments, bool requiresApproval);
typedef OnCommandRequest = void Function(String commandId, String command, String explanation, bool isBackground);
typedef OnError = void Function(String error, String details);
typedef OnIteration = void Function(int current, int max);
typedef OnStreamEnd = void Function();

class NeoCoreGrpcClient {
  static const _tag = 'GrpcClient';

  ClientChannel? _channel;
  NeoDesktopServiceClient? _stub;
  StreamController<DesktopMessage>? _requestController;
  StreamSubscription<CoreMessage>? _responseSubscription;
  Timer? _keepAliveTimer;
  String? _activeSessionId;
  String? _activeUserId;

  bool _connected = false;
  bool get isConnected => _connected;
  String? get activeSessionId => _activeSessionId;
  String? get activeUserId => _activeUserId;

  // Callbacks
  OnThinking? onThinking;
  OnResponse? onResponse;
  OnToolRequest? onToolRequest;
  OnCommandRequest? onCommandRequest;
  OnError? onError;
  OnIteration? onIteration;
  OnStreamEnd? onStreamEnd;

  Future<void> connect(String host, int port) async {
    if (_connected) await disconnect();

    Log.i(_tag, 'Connecting to $host:$port');
    _channel = ClientChannel(
      host,
      port: port,
      options: const ChannelOptions(
        credentials: ChannelCredentials.insecure(),
        connectionTimeout: Duration(seconds: 10),
      ),
    );
    _stub = NeoDesktopServiceClient(_channel!);
    _connected = true;
    Log.i(_tag, 'Connected');
  }

  Future<void> disconnect() async {
    Log.i(_tag, 'Disconnecting');
    _keepAliveTimer?.cancel();
    _keepAliveTimer = null;
    await _responseSubscription?.cancel();
    _responseSubscription = null;
    await _requestController?.close();
    _requestController = null;
    await _channel?.shutdown();
    _channel = null;
    _stub = null;
    _connected = false;
    _activeSessionId = null;
  }

  Future<bool> healthCheck() async {
    if (_stub == null) return false;
    try {
      final response = await _stub!.healthCheck(
        HealthCheckRequest(),
        options: CallOptions(timeout: const Duration(seconds: 5)),
      );
      Log.i(_tag, 'Health: ${response.healthy} v${response.version}');
      return response.healthy;
    } catch (e) {
      Log.e(_tag, 'Health check failed', e);
      return false;
    }
  }

  void executeTask(String sessionId, String message, {String? userId, Map<String, String>? metadata}) {
    if (_stub == null) {
      onError?.call('Not connected', 'Call connect() first');
      return;
    }

    _activeSessionId = sessionId;
    _activeUserId = userId;

    // Close any existing stream
    _responseSubscription?.cancel();
    _requestController?.close();

    _requestController = StreamController<DesktopMessage>();

    final responseStream = _stub!.executeTask(_requestController!.stream);

    _responseSubscription = responseStream.listen(
      _handleCoreMessage,
      onError: (Object e) {
        Log.e(_tag, 'Stream error', e);
        onError?.call('Stream error', e.toString());
      },
      onDone: () {
        Log.i(_tag, 'Stream ended');
        _keepAliveTimer?.cancel();
        onStreamEnd?.call();
      },
    );

    // Build metadata
    final meta = <String, String>{
      'platform': Platform.operatingSystem,
      'service': kServiceName,
      'timestamp': DateTime.now().toIso8601String(),
      ...?metadata,
    };

    // Send user input
    final msg = DesktopMessage()
      ..sessionId = sessionId
      ..userInput = (UserInput()
        ..message = message
        ..metadata.addAll(meta));
    if (userId != null && userId.isNotEmpty) msg.userId = userId;

    _requestController!.add(msg);
    Log.d(_tag, 'Sent message: ${message.length} chars');

    // Start keep-alive
    _startKeepAlive(sessionId);
  }

  void sendToolResult({
    required String toolCallId,
    required String toolName,
    required bool success,
    required String result,
    String? error,
  }) {
    if (_requestController == null || _activeSessionId == null) return;

    final msg = DesktopMessage()
      ..sessionId = _activeSessionId!
      ..toolResult = (ToolResult()
        ..toolCallId = toolCallId
        ..toolName = toolName
        ..success = success
        ..result = result
        ..error = error ?? '');
    if (_activeUserId != null && _activeUserId!.isNotEmpty) msg.userId = _activeUserId!;

    _requestController!.add(msg);
    Log.d(_tag, 'Sent tool result: $toolName ($success)');
  }

  void sendCommandApproval({
    required String commandId,
    required bool approved,
  }) {
    if (_requestController == null || _activeSessionId == null) return;

    final msg = DesktopMessage()
      ..sessionId = _activeSessionId!
      ..commandApproval = (CommandApproval()
        ..commandId = commandId
        ..approved = approved);
    if (_activeUserId != null && _activeUserId!.isNotEmpty) msg.userId = _activeUserId!;

    _requestController!.add(msg);
    Log.d(_tag, 'Sent command approval: $commandId ($approved)');
  }

  void _handleCoreMessage(CoreMessage msg) {
    if (msg.hasThinking()) {
      final t = msg.thinking;
      Log.d(_tag, 'Thinking: ${t.message.substring(0, t.message.length.clamp(0, 80))}');
      onThinking?.call(t.message, t.iteration);
    } else if (msg.hasToolRequest()) {
      final t = msg.toolRequest;
      Log.d(_tag, 'Tool request: ${t.toolName}');
      onToolRequest?.call(t.toolCallId, t.toolName, t.arguments, t.requiresApproval);
    } else if (msg.hasCommandRequest()) {
      final c = msg.commandRequest;
      Log.d(_tag, 'Command request: ${c.command}');
      onCommandRequest?.call(c.commandId, c.command, c.explanation, c.isBackground);
    } else if (msg.hasResponse()) {
      final r = msg.response;
      Log.d(_tag, 'Response: final=${r.isFinal}, iterations=${r.totalIterations}');
      _activeSessionId = msg.sessionId;
      onResponse?.call(r.message, r.isFinal, r.totalIterations);
    } else if (msg.hasError()) {
      final e = msg.error;
      Log.e(_tag, 'Error: ${e.error}');
      onError?.call(e.error, e.details);
    } else if (msg.hasIteration()) {
      final i = msg.iteration;
      Log.d(_tag, 'Iteration: ${i.current}/${i.max}');
      onIteration?.call(i.current, i.max);
    }
  }

  void _startKeepAlive(String sessionId) {
    _keepAliveTimer?.cancel();
    _keepAliveTimer = Timer.periodic(kKeepAliveInterval, (_) {
      if (_requestController == null) return;
      final msg = DesktopMessage()
        ..sessionId = sessionId
        ..keepAlive = (KeepAlive()
          ..timestamp = Int64(DateTime.now().millisecondsSinceEpoch));
      _requestController!.add(msg);
    });
  }
}
