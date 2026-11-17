//
//  Generated code. Do not modify.
//  source: neo_desktop.proto
//
// @dart = 2.12

// ignore_for_file: annotate_overrides, camel_case_types, comment_references
// ignore_for_file: constant_identifier_names, library_prefixes
// ignore_for_file: non_constant_identifier_names, prefer_final_fields
// ignore_for_file: unnecessary_import, unnecessary_this, unused_import

import 'dart:convert' as $convert;
import 'dart:core' as $core;
import 'dart:typed_data' as $typed_data;

@$core.Deprecated('Use desktopMessageDescriptor instead')
const DesktopMessage$json = {
  '1': 'DesktopMessage',
  '2': [
    {'1': 'session_id', '3': 1, '4': 1, '5': 9, '10': 'sessionId'},
    {'1': 'user_id', '3': 6, '4': 1, '5': 9, '10': 'userId'},
    {'1': 'user_input', '3': 2, '4': 1, '5': 11, '6': '.neo.desktop.UserInput', '9': 0, '10': 'userInput'},
    {'1': 'tool_result', '3': 3, '4': 1, '5': 11, '6': '.neo.desktop.ToolResult', '9': 0, '10': 'toolResult'},
    {'1': 'command_approval', '3': 4, '4': 1, '5': 11, '6': '.neo.desktop.CommandApproval', '9': 0, '10': 'commandApproval'},
    {'1': 'keep_alive', '3': 5, '4': 1, '5': 11, '6': '.neo.desktop.KeepAlive', '9': 0, '10': 'keepAlive'},
  ],
  '8': [
    {'1': 'content'},
  ],
};

/// Descriptor for `DesktopMessage`. Decode as a `google.protobuf.DescriptorProto`.
final $typed_data.Uint8List desktopMessageDescriptor = $convert.base64Decode(
    'Cg5EZXNrdG9wTWVzc2FnZRIdCgpzZXNzaW9uX2lkGAEgASgJUglzZXNzaW9uSWQSFwoHdXNlcl'
    '9pZBgGIAEoCVIGdXNlcklkEjcKCnVzZXJfaW5wdXQYAiABKAsyFi5uZW8uZGVza3RvcC5Vc2Vy'
    'SW5wdXRIAFIJdXNlcklucHV0EjoKC3Rvb2xfcmVzdWx0GAMgASgLMhcubmVvLmRlc2t0b3AuVG'
    '9vbFJlc3VsdEgAUgp0b29sUmVzdWx0EkkKEGNvbW1hbmRfYXBwcm92YWwYBCABKAsyHC5uZW8u'
    'ZGVza3RvcC5Db21tYW5kQXBwcm92YWxIAFIPY29tbWFuZEFwcHJvdmFsEjcKCmtlZXBfYWxpdm'
    'UYBSABKAsyFi5uZW8uZGVza3RvcC5LZWVwQWxpdmVIAFIJa2VlcEFsaXZlQgkKB2NvbnRlbnQ=');

@$core.Deprecated('Use userInputDescriptor instead')
const UserInput$json = {
  '1': 'UserInput',
  '2': [
    {'1': 'message', '3': 1, '4': 1, '5': 9, '10': 'message'},
    {'1': 'metadata', '3': 2, '4': 3, '5': 11, '6': '.neo.desktop.UserInput.MetadataEntry', '10': 'metadata'},
  ],
  '3': [UserInput_MetadataEntry$json],
};

@$core.Deprecated('Use userInputDescriptor instead')
const UserInput_MetadataEntry$json = {
  '1': 'MetadataEntry',
  '2': [
    {'1': 'key', '3': 1, '4': 1, '5': 9, '10': 'key'},
    {'1': 'value', '3': 2, '4': 1, '5': 9, '10': 'value'},
  ],
  '7': {'7': true},
};

/// Descriptor for `UserInput`. Decode as a `google.protobuf.DescriptorProto`.
final $typed_data.Uint8List userInputDescriptor = $convert.base64Decode(
    'CglVc2VySW5wdXQSGAoHbWVzc2FnZRgBIAEoCVIHbWVzc2FnZRJACghtZXRhZGF0YRgCIAMoCz'
    'IkLm5lby5kZXNrdG9wLlVzZXJJbnB1dC5NZXRhZGF0YUVudHJ5UghtZXRhZGF0YRo7Cg1NZXRh'
    'ZGF0YUVudHJ5EhAKA2tleRgBIAEoCVIDa2V5EhQKBXZhbHVlGAIgASgJUgV2YWx1ZToCOAE=');

@$core.Deprecated('Use toolResultDescriptor instead')
const ToolResult$json = {
  '1': 'ToolResult',
  '2': [
    {'1': 'tool_call_id', '3': 1, '4': 1, '5': 9, '10': 'toolCallId'},
    {'1': 'tool_name', '3': 2, '4': 1, '5': 9, '10': 'toolName'},
    {'1': 'success', '3': 3, '4': 1, '5': 8, '10': 'success'},
    {'1': 'result', '3': 4, '4': 1, '5': 9, '10': 'result'},
    {'1': 'error', '3': 5, '4': 1, '5': 9, '10': 'error'},
  ],
};

/// Descriptor for `ToolResult`. Decode as a `google.protobuf.DescriptorProto`.
final $typed_data.Uint8List toolResultDescriptor = $convert.base64Decode(
    'CgpUb29sUmVzdWx0EiAKDHRvb2xfY2FsbF9pZBgBIAEoCVIKdG9vbENhbGxJZBIbCgl0b29sX2'
    '5hbWUYAiABKAlSCHRvb2xOYW1lEhgKB3N1Y2Nlc3MYAyABKAhSB3N1Y2Nlc3MSFgoGcmVzdWx0'
    'GAQgASgJUgZyZXN1bHQSFAoFZXJyb3IYBSABKAlSBWVycm9y');

@$core.Deprecated('Use commandApprovalDescriptor instead')
const CommandApproval$json = {
  '1': 'CommandApproval',
  '2': [
    {'1': 'command_id', '3': 1, '4': 1, '5': 9, '10': 'commandId'},
    {'1': 'approved', '3': 2, '4': 1, '5': 8, '10': 'approved'},
  ],
};

/// Descriptor for `CommandApproval`. Decode as a `google.protobuf.DescriptorProto`.
final $typed_data.Uint8List commandApprovalDescriptor = $convert.base64Decode(
    'Cg9Db21tYW5kQXBwcm92YWwSHQoKY29tbWFuZF9pZBgBIAEoCVIJY29tbWFuZElkEhoKCGFwcH'
    'JvdmVkGAIgASgIUghhcHByb3ZlZA==');

@$core.Deprecated('Use keepAliveDescriptor instead')
const KeepAlive$json = {
  '1': 'KeepAlive',
  '2': [
    {'1': 'timestamp', '3': 1, '4': 1, '5': 3, '10': 'timestamp'},
  ],
};

/// Descriptor for `KeepAlive`. Decode as a `google.protobuf.DescriptorProto`.
final $typed_data.Uint8List keepAliveDescriptor = $convert.base64Decode(
    'CglLZWVwQWxpdmUSHAoJdGltZXN0YW1wGAEgASgDUgl0aW1lc3RhbXA=');

@$core.Deprecated('Use coreMessageDescriptor instead')
const CoreMessage$json = {
  '1': 'CoreMessage',
  '2': [
    {'1': 'session_id', '3': 1, '4': 1, '5': 9, '10': 'sessionId'},
    {'1': 'thinking', '3': 2, '4': 1, '5': 11, '6': '.neo.desktop.AgentThinking', '9': 0, '10': 'thinking'},
    {'1': 'tool_request', '3': 3, '4': 1, '5': 11, '6': '.neo.desktop.ToolRequest', '9': 0, '10': 'toolRequest'},
    {'1': 'command_request', '3': 4, '4': 1, '5': 11, '6': '.neo.desktop.CommandRequest', '9': 0, '10': 'commandRequest'},
    {'1': 'response', '3': 5, '4': 1, '5': 11, '6': '.neo.desktop.AgentResponse', '9': 0, '10': 'response'},
    {'1': 'error', '3': 6, '4': 1, '5': 11, '6': '.neo.desktop.ErrorMessage', '9': 0, '10': 'error'},
    {'1': 'iteration', '3': 7, '4': 1, '5': 11, '6': '.neo.desktop.IterationUpdate', '9': 0, '10': 'iteration'},
  ],
  '8': [
    {'1': 'content'},
  ],
};

/// Descriptor for `CoreMessage`. Decode as a `google.protobuf.DescriptorProto`.
final $typed_data.Uint8List coreMessageDescriptor = $convert.base64Decode(
    'CgtDb3JlTWVzc2FnZRIdCgpzZXNzaW9uX2lkGAEgASgJUglzZXNzaW9uSWQSOAoIdGhpbmtpbm'
    'cYAiABKAsyGi5uZW8uZGVza3RvcC5BZ2VudFRoaW5raW5nSABSCHRoaW5raW5nEj0KDHRvb2xf'
    'cmVxdWVzdBgDIAEoCzIYLm5lby5kZXNrdG9wLlRvb2xSZXF1ZXN0SABSC3Rvb2xSZXF1ZXN0Ek'
    'YKD2NvbW1hbmRfcmVxdWVzdBgEIAEoCzIbLm5lby5kZXNrdG9wLkNvbW1hbmRSZXF1ZXN0SABS'
    'DmNvbW1hbmRSZXF1ZXN0EjgKCHJlc3BvbnNlGAUgASgLMhoubmVvLmRlc2t0b3AuQWdlbnRSZX'
    'Nwb25zZUgAUghyZXNwb25zZRIxCgVlcnJvchgGIAEoCzIZLm5lby5kZXNrdG9wLkVycm9yTWVz'
    'c2FnZUgAUgVlcnJvchI8CglpdGVyYXRpb24YByABKAsyHC5uZW8uZGVza3RvcC5JdGVyYXRpb2'
    '5VcGRhdGVIAFIJaXRlcmF0aW9uQgkKB2NvbnRlbnQ=');

@$core.Deprecated('Use agentThinkingDescriptor instead')
const AgentThinking$json = {
  '1': 'AgentThinking',
  '2': [
    {'1': 'message', '3': 1, '4': 1, '5': 9, '10': 'message'},
    {'1': 'iteration', '3': 2, '4': 1, '5': 5, '10': 'iteration'},
  ],
};

/// Descriptor for `AgentThinking`. Decode as a `google.protobuf.DescriptorProto`.
final $typed_data.Uint8List agentThinkingDescriptor = $convert.base64Decode(
    'Cg1BZ2VudFRoaW5raW5nEhgKB21lc3NhZ2UYASABKAlSB21lc3NhZ2USHAoJaXRlcmF0aW9uGA'
    'IgASgFUglpdGVyYXRpb24=');

@$core.Deprecated('Use toolRequestDescriptor instead')
const ToolRequest$json = {
  '1': 'ToolRequest',
  '2': [
    {'1': 'tool_call_id', '3': 1, '4': 1, '5': 9, '10': 'toolCallId'},
    {'1': 'tool_name', '3': 2, '4': 1, '5': 9, '10': 'toolName'},
    {'1': 'arguments', '3': 3, '4': 1, '5': 9, '10': 'arguments'},
    {'1': 'requires_approval', '3': 4, '4': 1, '5': 8, '10': 'requiresApproval'},
  ],
};

/// Descriptor for `ToolRequest`. Decode as a `google.protobuf.DescriptorProto`.
final $typed_data.Uint8List toolRequestDescriptor = $convert.base64Decode(
    'CgtUb29sUmVxdWVzdBIgCgx0b29sX2NhbGxfaWQYASABKAlSCnRvb2xDYWxsSWQSGwoJdG9vbF'
    '9uYW1lGAIgASgJUgh0b29sTmFtZRIcCglhcmd1bWVudHMYAyABKAlSCWFyZ3VtZW50cxIrChFy'
    'ZXF1aXJlc19hcHByb3ZhbBgEIAEoCFIQcmVxdWlyZXNBcHByb3ZhbA==');

@$core.Deprecated('Use commandRequestDescriptor instead')
const CommandRequest$json = {
  '1': 'CommandRequest',
  '2': [
    {'1': 'command_id', '3': 1, '4': 1, '5': 9, '10': 'commandId'},
    {'1': 'command', '3': 2, '4': 1, '5': 9, '10': 'command'},
    {'1': 'explanation', '3': 3, '4': 1, '5': 9, '10': 'explanation'},
    {'1': 'is_background', '3': 4, '4': 1, '5': 8, '10': 'isBackground'},
  ],
};

/// Descriptor for `CommandRequest`. Decode as a `google.protobuf.DescriptorProto`.
final $typed_data.Uint8List commandRequestDescriptor = $convert.base64Decode(
    'Cg5Db21tYW5kUmVxdWVzdBIdCgpjb21tYW5kX2lkGAEgASgJUgljb21tYW5kSWQSGAoHY29tbW'
    'FuZBgCIAEoCVIHY29tbWFuZBIgCgtleHBsYW5hdGlvbhgDIAEoCVILZXhwbGFuYXRpb24SIwoN'
    'aXNfYmFja2dyb3VuZBgEIAEoCFIMaXNCYWNrZ3JvdW5k');

@$core.Deprecated('Use agentResponseDescriptor instead')
const AgentResponse$json = {
  '1': 'AgentResponse',
  '2': [
    {'1': 'message', '3': 1, '4': 1, '5': 9, '10': 'message'},
    {'1': 'is_final', '3': 2, '4': 1, '5': 8, '10': 'isFinal'},
    {'1': 'total_iterations', '3': 3, '4': 1, '5': 5, '10': 'totalIterations'},
  ],
};

/// Descriptor for `AgentResponse`. Decode as a `google.protobuf.DescriptorProto`.
final $typed_data.Uint8List agentResponseDescriptor = $convert.base64Decode(
    'Cg1BZ2VudFJlc3BvbnNlEhgKB21lc3NhZ2UYASABKAlSB21lc3NhZ2USGQoIaXNfZmluYWwYAi'
    'ABKAhSB2lzRmluYWwSKQoQdG90YWxfaXRlcmF0aW9ucxgDIAEoBVIPdG90YWxJdGVyYXRpb25z');

@$core.Deprecated('Use errorMessageDescriptor instead')
const ErrorMessage$json = {
  '1': 'ErrorMessage',
  '2': [
    {'1': 'error', '3': 1, '4': 1, '5': 9, '10': 'error'},
    {'1': 'details', '3': 2, '4': 1, '5': 9, '10': 'details'},
  ],
};

/// Descriptor for `ErrorMessage`. Decode as a `google.protobuf.DescriptorProto`.
final $typed_data.Uint8List errorMessageDescriptor = $convert.base64Decode(
    'CgxFcnJvck1lc3NhZ2USFAoFZXJyb3IYASABKAlSBWVycm9yEhgKB2RldGFpbHMYAiABKAlSB2'
    'RldGFpbHM=');

@$core.Deprecated('Use iterationUpdateDescriptor instead')
const IterationUpdate$json = {
  '1': 'IterationUpdate',
  '2': [
    {'1': 'current', '3': 1, '4': 1, '5': 5, '10': 'current'},
    {'1': 'max', '3': 2, '4': 1, '5': 5, '10': 'max'},
  ],
};

/// Descriptor for `IterationUpdate`. Decode as a `google.protobuf.DescriptorProto`.
final $typed_data.Uint8List iterationUpdateDescriptor = $convert.base64Decode(
    'Cg9JdGVyYXRpb25VcGRhdGUSGAoHY3VycmVudBgBIAEoBVIHY3VycmVudBIQCgNtYXgYAiABKA'
    'VSA21heA==');

@$core.Deprecated('Use healthCheckRequestDescriptor instead')
const HealthCheckRequest$json = {
  '1': 'HealthCheckRequest',
};

/// Descriptor for `HealthCheckRequest`. Decode as a `google.protobuf.DescriptorProto`.
final $typed_data.Uint8List healthCheckRequestDescriptor = $convert.base64Decode(
    'ChJIZWFsdGhDaGVja1JlcXVlc3Q=');

@$core.Deprecated('Use healthCheckResponseDescriptor instead')
const HealthCheckResponse$json = {
  '1': 'HealthCheckResponse',
  '2': [
    {'1': 'healthy', '3': 1, '4': 1, '5': 8, '10': 'healthy'},
    {'1': 'version', '3': 2, '4': 1, '5': 9, '10': 'version'},
    {'1': 'status', '3': 3, '4': 3, '5': 11, '6': '.neo.desktop.HealthCheckResponse.StatusEntry', '10': 'status'},
  ],
  '3': [HealthCheckResponse_StatusEntry$json],
};

@$core.Deprecated('Use healthCheckResponseDescriptor instead')
const HealthCheckResponse_StatusEntry$json = {
  '1': 'StatusEntry',
  '2': [
    {'1': 'key', '3': 1, '4': 1, '5': 9, '10': 'key'},
    {'1': 'value', '3': 2, '4': 1, '5': 9, '10': 'value'},
  ],
  '7': {'7': true},
};

/// Descriptor for `HealthCheckResponse`. Decode as a `google.protobuf.DescriptorProto`.
final $typed_data.Uint8List healthCheckResponseDescriptor = $convert.base64Decode(
    'ChNIZWFsdGhDaGVja1Jlc3BvbnNlEhgKB2hlYWx0aHkYASABKAhSB2hlYWx0aHkSGAoHdmVyc2'
    'lvbhgCIAEoCVIHdmVyc2lvbhJECgZzdGF0dXMYAyADKAsyLC5uZW8uZGVza3RvcC5IZWFsdGhD'
    'aGVja1Jlc3BvbnNlLlN0YXR1c0VudHJ5UgZzdGF0dXMaOQoLU3RhdHVzRW50cnkSEAoDa2V5GA'
    'EgASgJUgNrZXkSFAoFdmFsdWUYAiABKAlSBXZhbHVlOgI4AQ==');

