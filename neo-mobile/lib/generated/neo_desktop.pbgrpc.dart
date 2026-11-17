//
//  Generated code. Do not modify.
//  source: neo_desktop.proto
//
// @dart = 2.12

// ignore_for_file: annotate_overrides, camel_case_types, comment_references
// ignore_for_file: constant_identifier_names, library_prefixes
// ignore_for_file: non_constant_identifier_names, prefer_final_fields
// ignore_for_file: unnecessary_import, unnecessary_this, unused_import

import 'dart:async' as $async;
import 'dart:core' as $core;

import 'package:grpc/service_api.dart' as $grpc;
import 'package:protobuf/protobuf.dart' as $pb;

import 'neo_desktop.pb.dart' as $0;

export 'neo_desktop.pb.dart';

@$pb.GrpcServiceName('neo.desktop.NeoDesktopService')
class NeoDesktopServiceClient extends $grpc.Client {
  static final _$executeTask = $grpc.ClientMethod<$0.DesktopMessage, $0.CoreMessage>(
      '/neo.desktop.NeoDesktopService/ExecuteTask',
      ($0.DesktopMessage value) => value.writeToBuffer(),
      ($core.List<$core.int> value) => $0.CoreMessage.fromBuffer(value));
  static final _$healthCheck = $grpc.ClientMethod<$0.HealthCheckRequest, $0.HealthCheckResponse>(
      '/neo.desktop.NeoDesktopService/HealthCheck',
      ($0.HealthCheckRequest value) => value.writeToBuffer(),
      ($core.List<$core.int> value) => $0.HealthCheckResponse.fromBuffer(value));

  NeoDesktopServiceClient($grpc.ClientChannel channel,
      {$grpc.CallOptions? options,
      $core.Iterable<$grpc.ClientInterceptor>? interceptors})
      : super(channel, options: options,
        interceptors: interceptors);

  $grpc.ResponseStream<$0.CoreMessage> executeTask($async.Stream<$0.DesktopMessage> request, {$grpc.CallOptions? options}) {
    return $createStreamingCall(_$executeTask, request, options: options);
  }

  $grpc.ResponseFuture<$0.HealthCheckResponse> healthCheck($0.HealthCheckRequest request, {$grpc.CallOptions? options}) {
    return $createUnaryCall(_$healthCheck, request, options: options);
  }
}

@$pb.GrpcServiceName('neo.desktop.NeoDesktopService')
abstract class NeoDesktopServiceBase extends $grpc.Service {
  $core.String get $name => 'neo.desktop.NeoDesktopService';

  NeoDesktopServiceBase() {
    $addMethod($grpc.ServiceMethod<$0.DesktopMessage, $0.CoreMessage>(
        'ExecuteTask',
        executeTask,
        true,
        true,
        ($core.List<$core.int> value) => $0.DesktopMessage.fromBuffer(value),
        ($0.CoreMessage value) => value.writeToBuffer()));
    $addMethod($grpc.ServiceMethod<$0.HealthCheckRequest, $0.HealthCheckResponse>(
        'HealthCheck',
        healthCheck_Pre,
        false,
        false,
        ($core.List<$core.int> value) => $0.HealthCheckRequest.fromBuffer(value),
        ($0.HealthCheckResponse value) => value.writeToBuffer()));
  }

  $async.Future<$0.HealthCheckResponse> healthCheck_Pre($grpc.ServiceCall call, $async.Future<$0.HealthCheckRequest> request) async {
    return healthCheck(call, await request);
  }

  $async.Stream<$0.CoreMessage> executeTask($grpc.ServiceCall call, $async.Stream<$0.DesktopMessage> request);
  $async.Future<$0.HealthCheckResponse> healthCheck($grpc.ServiceCall call, $0.HealthCheckRequest request);
}
