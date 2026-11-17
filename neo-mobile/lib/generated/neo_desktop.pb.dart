//
//  Generated code. Do not modify.
//  source: neo_desktop.proto
//
// @dart = 2.12

// ignore_for_file: annotate_overrides, camel_case_types, comment_references
// ignore_for_file: constant_identifier_names, library_prefixes
// ignore_for_file: non_constant_identifier_names, prefer_final_fields
// ignore_for_file: unnecessary_import, unnecessary_this, unused_import

import 'dart:core' as $core;

import 'package:fixnum/fixnum.dart' as $fixnum;
import 'package:protobuf/protobuf.dart' as $pb;

enum DesktopMessage_Content {
  userInput, 
  toolResult, 
  commandApproval, 
  keepAlive, 
  notSet
}

/// Mensagens do Desktop para o Core
class DesktopMessage extends $pb.GeneratedMessage {
  factory DesktopMessage({
    $core.String? sessionId,
    UserInput? userInput,
    ToolResult? toolResult,
    CommandApproval? commandApproval,
    KeepAlive? keepAlive,
    $core.String? userId,
  }) {
    final $result = create();
    if (sessionId != null) {
      $result.sessionId = sessionId;
    }
    if (userInput != null) {
      $result.userInput = userInput;
    }
    if (toolResult != null) {
      $result.toolResult = toolResult;
    }
    if (commandApproval != null) {
      $result.commandApproval = commandApproval;
    }
    if (keepAlive != null) {
      $result.keepAlive = keepAlive;
    }
    if (userId != null) {
      $result.userId = userId;
    }
    return $result;
  }
  DesktopMessage._() : super();
  factory DesktopMessage.fromBuffer($core.List<$core.int> i, [$pb.ExtensionRegistry r = $pb.ExtensionRegistry.EMPTY]) => create()..mergeFromBuffer(i, r);
  factory DesktopMessage.fromJson($core.String i, [$pb.ExtensionRegistry r = $pb.ExtensionRegistry.EMPTY]) => create()..mergeFromJson(i, r);

  static const $core.Map<$core.int, DesktopMessage_Content> _DesktopMessage_ContentByTag = {
    2 : DesktopMessage_Content.userInput,
    3 : DesktopMessage_Content.toolResult,
    4 : DesktopMessage_Content.commandApproval,
    5 : DesktopMessage_Content.keepAlive,
    0 : DesktopMessage_Content.notSet
  };
  static final $pb.BuilderInfo _i = $pb.BuilderInfo(_omitMessageNames ? '' : 'DesktopMessage', package: const $pb.PackageName(_omitMessageNames ? '' : 'neo.desktop'), createEmptyInstance: create)
    ..oo(0, [2, 3, 4, 5])
    ..aOS(1, _omitFieldNames ? '' : 'sessionId')
    ..aOM<UserInput>(2, _omitFieldNames ? '' : 'userInput', subBuilder: UserInput.create)
    ..aOM<ToolResult>(3, _omitFieldNames ? '' : 'toolResult', subBuilder: ToolResult.create)
    ..aOM<CommandApproval>(4, _omitFieldNames ? '' : 'commandApproval', subBuilder: CommandApproval.create)
    ..aOM<KeepAlive>(5, _omitFieldNames ? '' : 'keepAlive', subBuilder: KeepAlive.create)
    ..aOS(6, _omitFieldNames ? '' : 'userId')
    ..hasRequiredFields = false
  ;

  @$core.Deprecated(
  'Using this can add significant overhead to your binary. '
  'Use [GeneratedMessageGenericExtensions.deepCopy] instead. '
  'Will be removed in next major version')
  DesktopMessage clone() => DesktopMessage()..mergeFromMessage(this);
  @$core.Deprecated(
  'Using this can add significant overhead to your binary. '
  'Use [GeneratedMessageGenericExtensions.rebuild] instead. '
  'Will be removed in next major version')
  DesktopMessage copyWith(void Function(DesktopMessage) updates) => super.copyWith((message) => updates(message as DesktopMessage)) as DesktopMessage;

  $pb.BuilderInfo get info_ => _i;

  @$core.pragma('dart2js:noInline')
  static DesktopMessage create() => DesktopMessage._();
  DesktopMessage createEmptyInstance() => create();
  static $pb.PbList<DesktopMessage> createRepeated() => $pb.PbList<DesktopMessage>();
  @$core.pragma('dart2js:noInline')
  static DesktopMessage getDefault() => _defaultInstance ??= $pb.GeneratedMessage.$_defaultFor<DesktopMessage>(create);
  static DesktopMessage? _defaultInstance;

  DesktopMessage_Content whichContent() => _DesktopMessage_ContentByTag[$_whichOneof(0)]!;
  void clearContent() => clearField($_whichOneof(0));

  @$pb.TagNumber(1)
  $core.String get sessionId => $_getSZ(0);
  @$pb.TagNumber(1)
  set sessionId($core.String v) { $_setString(0, v); }
  @$pb.TagNumber(1)
  $core.bool hasSessionId() => $_has(0);
  @$pb.TagNumber(1)
  void clearSessionId() => clearField(1);

  @$pb.TagNumber(2)
  UserInput get userInput => $_getN(1);
  @$pb.TagNumber(2)
  set userInput(UserInput v) { setField(2, v); }
  @$pb.TagNumber(2)
  $core.bool hasUserInput() => $_has(1);
  @$pb.TagNumber(2)
  void clearUserInput() => clearField(2);
  @$pb.TagNumber(2)
  UserInput ensureUserInput() => $_ensure(1);

  @$pb.TagNumber(3)
  ToolResult get toolResult => $_getN(2);
  @$pb.TagNumber(3)
  set toolResult(ToolResult v) { setField(3, v); }
  @$pb.TagNumber(3)
  $core.bool hasToolResult() => $_has(2);
  @$pb.TagNumber(3)
  void clearToolResult() => clearField(3);
  @$pb.TagNumber(3)
  ToolResult ensureToolResult() => $_ensure(2);

  @$pb.TagNumber(4)
  CommandApproval get commandApproval => $_getN(3);
  @$pb.TagNumber(4)
  set commandApproval(CommandApproval v) { setField(4, v); }
  @$pb.TagNumber(4)
  $core.bool hasCommandApproval() => $_has(3);
  @$pb.TagNumber(4)
  void clearCommandApproval() => clearField(4);
  @$pb.TagNumber(4)
  CommandApproval ensureCommandApproval() => $_ensure(3);

  @$pb.TagNumber(5)
  KeepAlive get keepAlive => $_getN(4);
  @$pb.TagNumber(5)
  set keepAlive(KeepAlive v) { setField(5, v); }
  @$pb.TagNumber(5)
  $core.bool hasKeepAlive() => $_has(4);
  @$pb.TagNumber(5)
  void clearKeepAlive() => clearField(5);
  @$pb.TagNumber(5)
  KeepAlive ensureKeepAlive() => $_ensure(4);

  @$pb.TagNumber(6)
  $core.String get userId => $_getSZ(5);
  @$pb.TagNumber(6)
  set userId($core.String v) { $_setString(5, v); }
  @$pb.TagNumber(6)
  $core.bool hasUserId() => $_has(5);
  @$pb.TagNumber(6)
  void clearUserId() => clearField(6);
}

class UserInput extends $pb.GeneratedMessage {
  factory UserInput({
    $core.String? message,
    $core.Map<$core.String, $core.String>? metadata,
  }) {
    final $result = create();
    if (message != null) {
      $result.message = message;
    }
    if (metadata != null) {
      $result.metadata.addAll(metadata);
    }
    return $result;
  }
  UserInput._() : super();
  factory UserInput.fromBuffer($core.List<$core.int> i, [$pb.ExtensionRegistry r = $pb.ExtensionRegistry.EMPTY]) => create()..mergeFromBuffer(i, r);
  factory UserInput.fromJson($core.String i, [$pb.ExtensionRegistry r = $pb.ExtensionRegistry.EMPTY]) => create()..mergeFromJson(i, r);

  static final $pb.BuilderInfo _i = $pb.BuilderInfo(_omitMessageNames ? '' : 'UserInput', package: const $pb.PackageName(_omitMessageNames ? '' : 'neo.desktop'), createEmptyInstance: create)
    ..aOS(1, _omitFieldNames ? '' : 'message')
    ..m<$core.String, $core.String>(2, _omitFieldNames ? '' : 'metadata', entryClassName: 'UserInput.MetadataEntry', keyFieldType: $pb.PbFieldType.OS, valueFieldType: $pb.PbFieldType.OS, packageName: const $pb.PackageName('neo.desktop'))
    ..hasRequiredFields = false
  ;

  @$core.Deprecated(
  'Using this can add significant overhead to your binary. '
  'Use [GeneratedMessageGenericExtensions.deepCopy] instead. '
  'Will be removed in next major version')
  UserInput clone() => UserInput()..mergeFromMessage(this);
  @$core.Deprecated(
  'Using this can add significant overhead to your binary. '
  'Use [GeneratedMessageGenericExtensions.rebuild] instead. '
  'Will be removed in next major version')
  UserInput copyWith(void Function(UserInput) updates) => super.copyWith((message) => updates(message as UserInput)) as UserInput;

  $pb.BuilderInfo get info_ => _i;

  @$core.pragma('dart2js:noInline')
  static UserInput create() => UserInput._();
  UserInput createEmptyInstance() => create();
  static $pb.PbList<UserInput> createRepeated() => $pb.PbList<UserInput>();
  @$core.pragma('dart2js:noInline')
  static UserInput getDefault() => _defaultInstance ??= $pb.GeneratedMessage.$_defaultFor<UserInput>(create);
  static UserInput? _defaultInstance;

  @$pb.TagNumber(1)
  $core.String get message => $_getSZ(0);
  @$pb.TagNumber(1)
  set message($core.String v) { $_setString(0, v); }
  @$pb.TagNumber(1)
  $core.bool hasMessage() => $_has(0);
  @$pb.TagNumber(1)
  void clearMessage() => clearField(1);

  @$pb.TagNumber(2)
  $core.Map<$core.String, $core.String> get metadata => $_getMap(1);
}

class ToolResult extends $pb.GeneratedMessage {
  factory ToolResult({
    $core.String? toolCallId,
    $core.String? toolName,
    $core.bool? success,
    $core.String? result,
    $core.String? error,
  }) {
    final $result = create();
    if (toolCallId != null) {
      $result.toolCallId = toolCallId;
    }
    if (toolName != null) {
      $result.toolName = toolName;
    }
    if (success != null) {
      $result.success = success;
    }
    if (result != null) {
      $result.result = result;
    }
    if (error != null) {
      $result.error = error;
    }
    return $result;
  }
  ToolResult._() : super();
  factory ToolResult.fromBuffer($core.List<$core.int> i, [$pb.ExtensionRegistry r = $pb.ExtensionRegistry.EMPTY]) => create()..mergeFromBuffer(i, r);
  factory ToolResult.fromJson($core.String i, [$pb.ExtensionRegistry r = $pb.ExtensionRegistry.EMPTY]) => create()..mergeFromJson(i, r);

  static final $pb.BuilderInfo _i = $pb.BuilderInfo(_omitMessageNames ? '' : 'ToolResult', package: const $pb.PackageName(_omitMessageNames ? '' : 'neo.desktop'), createEmptyInstance: create)
    ..aOS(1, _omitFieldNames ? '' : 'toolCallId')
    ..aOS(2, _omitFieldNames ? '' : 'toolName')
    ..aOB(3, _omitFieldNames ? '' : 'success')
    ..aOS(4, _omitFieldNames ? '' : 'result')
    ..aOS(5, _omitFieldNames ? '' : 'error')
    ..hasRequiredFields = false
  ;

  @$core.Deprecated(
  'Using this can add significant overhead to your binary. '
  'Use [GeneratedMessageGenericExtensions.deepCopy] instead. '
  'Will be removed in next major version')
  ToolResult clone() => ToolResult()..mergeFromMessage(this);
  @$core.Deprecated(
  'Using this can add significant overhead to your binary. '
  'Use [GeneratedMessageGenericExtensions.rebuild] instead. '
  'Will be removed in next major version')
  ToolResult copyWith(void Function(ToolResult) updates) => super.copyWith((message) => updates(message as ToolResult)) as ToolResult;

  $pb.BuilderInfo get info_ => _i;

  @$core.pragma('dart2js:noInline')
  static ToolResult create() => ToolResult._();
  ToolResult createEmptyInstance() => create();
  static $pb.PbList<ToolResult> createRepeated() => $pb.PbList<ToolResult>();
  @$core.pragma('dart2js:noInline')
  static ToolResult getDefault() => _defaultInstance ??= $pb.GeneratedMessage.$_defaultFor<ToolResult>(create);
  static ToolResult? _defaultInstance;

  @$pb.TagNumber(1)
  $core.String get toolCallId => $_getSZ(0);
  @$pb.TagNumber(1)
  set toolCallId($core.String v) { $_setString(0, v); }
  @$pb.TagNumber(1)
  $core.bool hasToolCallId() => $_has(0);
  @$pb.TagNumber(1)
  void clearToolCallId() => clearField(1);

  @$pb.TagNumber(2)
  $core.String get toolName => $_getSZ(1);
  @$pb.TagNumber(2)
  set toolName($core.String v) { $_setString(1, v); }
  @$pb.TagNumber(2)
  $core.bool hasToolName() => $_has(1);
  @$pb.TagNumber(2)
  void clearToolName() => clearField(2);

  @$pb.TagNumber(3)
  $core.bool get success => $_getBF(2);
  @$pb.TagNumber(3)
  set success($core.bool v) { $_setBool(2, v); }
  @$pb.TagNumber(3)
  $core.bool hasSuccess() => $_has(2);
  @$pb.TagNumber(3)
  void clearSuccess() => clearField(3);

  @$pb.TagNumber(4)
  $core.String get result => $_getSZ(3);
  @$pb.TagNumber(4)
  set result($core.String v) { $_setString(3, v); }
  @$pb.TagNumber(4)
  $core.bool hasResult() => $_has(3);
  @$pb.TagNumber(4)
  void clearResult() => clearField(4);

  @$pb.TagNumber(5)
  $core.String get error => $_getSZ(4);
  @$pb.TagNumber(5)
  set error($core.String v) { $_setString(4, v); }
  @$pb.TagNumber(5)
  $core.bool hasError() => $_has(4);
  @$pb.TagNumber(5)
  void clearError() => clearField(5);
}

class CommandApproval extends $pb.GeneratedMessage {
  factory CommandApproval({
    $core.String? commandId,
    $core.bool? approved,
  }) {
    final $result = create();
    if (commandId != null) {
      $result.commandId = commandId;
    }
    if (approved != null) {
      $result.approved = approved;
    }
    return $result;
  }
  CommandApproval._() : super();
  factory CommandApproval.fromBuffer($core.List<$core.int> i, [$pb.ExtensionRegistry r = $pb.ExtensionRegistry.EMPTY]) => create()..mergeFromBuffer(i, r);
  factory CommandApproval.fromJson($core.String i, [$pb.ExtensionRegistry r = $pb.ExtensionRegistry.EMPTY]) => create()..mergeFromJson(i, r);

  static final $pb.BuilderInfo _i = $pb.BuilderInfo(_omitMessageNames ? '' : 'CommandApproval', package: const $pb.PackageName(_omitMessageNames ? '' : 'neo.desktop'), createEmptyInstance: create)
    ..aOS(1, _omitFieldNames ? '' : 'commandId')
    ..aOB(2, _omitFieldNames ? '' : 'approved')
    ..hasRequiredFields = false
  ;

  @$core.Deprecated(
  'Using this can add significant overhead to your binary. '
  'Use [GeneratedMessageGenericExtensions.deepCopy] instead. '
  'Will be removed in next major version')
  CommandApproval clone() => CommandApproval()..mergeFromMessage(this);
  @$core.Deprecated(
  'Using this can add significant overhead to your binary. '
  'Use [GeneratedMessageGenericExtensions.rebuild] instead. '
  'Will be removed in next major version')
  CommandApproval copyWith(void Function(CommandApproval) updates) => super.copyWith((message) => updates(message as CommandApproval)) as CommandApproval;

  $pb.BuilderInfo get info_ => _i;

  @$core.pragma('dart2js:noInline')
  static CommandApproval create() => CommandApproval._();
  CommandApproval createEmptyInstance() => create();
  static $pb.PbList<CommandApproval> createRepeated() => $pb.PbList<CommandApproval>();
  @$core.pragma('dart2js:noInline')
  static CommandApproval getDefault() => _defaultInstance ??= $pb.GeneratedMessage.$_defaultFor<CommandApproval>(create);
  static CommandApproval? _defaultInstance;

  @$pb.TagNumber(1)
  $core.String get commandId => $_getSZ(0);
  @$pb.TagNumber(1)
  set commandId($core.String v) { $_setString(0, v); }
  @$pb.TagNumber(1)
  $core.bool hasCommandId() => $_has(0);
  @$pb.TagNumber(1)
  void clearCommandId() => clearField(1);

  @$pb.TagNumber(2)
  $core.bool get approved => $_getBF(1);
  @$pb.TagNumber(2)
  set approved($core.bool v) { $_setBool(1, v); }
  @$pb.TagNumber(2)
  $core.bool hasApproved() => $_has(1);
  @$pb.TagNumber(2)
  void clearApproved() => clearField(2);
}

class KeepAlive extends $pb.GeneratedMessage {
  factory KeepAlive({
    $fixnum.Int64? timestamp,
  }) {
    final $result = create();
    if (timestamp != null) {
      $result.timestamp = timestamp;
    }
    return $result;
  }
  KeepAlive._() : super();
  factory KeepAlive.fromBuffer($core.List<$core.int> i, [$pb.ExtensionRegistry r = $pb.ExtensionRegistry.EMPTY]) => create()..mergeFromBuffer(i, r);
  factory KeepAlive.fromJson($core.String i, [$pb.ExtensionRegistry r = $pb.ExtensionRegistry.EMPTY]) => create()..mergeFromJson(i, r);

  static final $pb.BuilderInfo _i = $pb.BuilderInfo(_omitMessageNames ? '' : 'KeepAlive', package: const $pb.PackageName(_omitMessageNames ? '' : 'neo.desktop'), createEmptyInstance: create)
    ..aInt64(1, _omitFieldNames ? '' : 'timestamp')
    ..hasRequiredFields = false
  ;

  @$core.Deprecated(
  'Using this can add significant overhead to your binary. '
  'Use [GeneratedMessageGenericExtensions.deepCopy] instead. '
  'Will be removed in next major version')
  KeepAlive clone() => KeepAlive()..mergeFromMessage(this);
  @$core.Deprecated(
  'Using this can add significant overhead to your binary. '
  'Use [GeneratedMessageGenericExtensions.rebuild] instead. '
  'Will be removed in next major version')
  KeepAlive copyWith(void Function(KeepAlive) updates) => super.copyWith((message) => updates(message as KeepAlive)) as KeepAlive;

  $pb.BuilderInfo get info_ => _i;

  @$core.pragma('dart2js:noInline')
  static KeepAlive create() => KeepAlive._();
  KeepAlive createEmptyInstance() => create();
  static $pb.PbList<KeepAlive> createRepeated() => $pb.PbList<KeepAlive>();
  @$core.pragma('dart2js:noInline')
  static KeepAlive getDefault() => _defaultInstance ??= $pb.GeneratedMessage.$_defaultFor<KeepAlive>(create);
  static KeepAlive? _defaultInstance;

  @$pb.TagNumber(1)
  $fixnum.Int64 get timestamp => $_getI64(0);
  @$pb.TagNumber(1)
  set timestamp($fixnum.Int64 v) { $_setInt64(0, v); }
  @$pb.TagNumber(1)
  $core.bool hasTimestamp() => $_has(0);
  @$pb.TagNumber(1)
  void clearTimestamp() => clearField(1);
}

enum CoreMessage_Content {
  thinking, 
  toolRequest, 
  commandRequest, 
  response, 
  error, 
  iteration, 
  notSet
}

/// Mensagens do Core para o Desktop
class CoreMessage extends $pb.GeneratedMessage {
  factory CoreMessage({
    $core.String? sessionId,
    AgentThinking? thinking,
    ToolRequest? toolRequest,
    CommandRequest? commandRequest,
    AgentResponse? response,
    ErrorMessage? error,
    IterationUpdate? iteration,
  }) {
    final $result = create();
    if (sessionId != null) {
      $result.sessionId = sessionId;
    }
    if (thinking != null) {
      $result.thinking = thinking;
    }
    if (toolRequest != null) {
      $result.toolRequest = toolRequest;
    }
    if (commandRequest != null) {
      $result.commandRequest = commandRequest;
    }
    if (response != null) {
      $result.response = response;
    }
    if (error != null) {
      $result.error = error;
    }
    if (iteration != null) {
      $result.iteration = iteration;
    }
    return $result;
  }
  CoreMessage._() : super();
  factory CoreMessage.fromBuffer($core.List<$core.int> i, [$pb.ExtensionRegistry r = $pb.ExtensionRegistry.EMPTY]) => create()..mergeFromBuffer(i, r);
  factory CoreMessage.fromJson($core.String i, [$pb.ExtensionRegistry r = $pb.ExtensionRegistry.EMPTY]) => create()..mergeFromJson(i, r);

  static const $core.Map<$core.int, CoreMessage_Content> _CoreMessage_ContentByTag = {
    2 : CoreMessage_Content.thinking,
    3 : CoreMessage_Content.toolRequest,
    4 : CoreMessage_Content.commandRequest,
    5 : CoreMessage_Content.response,
    6 : CoreMessage_Content.error,
    7 : CoreMessage_Content.iteration,
    0 : CoreMessage_Content.notSet
  };
  static final $pb.BuilderInfo _i = $pb.BuilderInfo(_omitMessageNames ? '' : 'CoreMessage', package: const $pb.PackageName(_omitMessageNames ? '' : 'neo.desktop'), createEmptyInstance: create)
    ..oo(0, [2, 3, 4, 5, 6, 7])
    ..aOS(1, _omitFieldNames ? '' : 'sessionId')
    ..aOM<AgentThinking>(2, _omitFieldNames ? '' : 'thinking', subBuilder: AgentThinking.create)
    ..aOM<ToolRequest>(3, _omitFieldNames ? '' : 'toolRequest', subBuilder: ToolRequest.create)
    ..aOM<CommandRequest>(4, _omitFieldNames ? '' : 'commandRequest', subBuilder: CommandRequest.create)
    ..aOM<AgentResponse>(5, _omitFieldNames ? '' : 'response', subBuilder: AgentResponse.create)
    ..aOM<ErrorMessage>(6, _omitFieldNames ? '' : 'error', subBuilder: ErrorMessage.create)
    ..aOM<IterationUpdate>(7, _omitFieldNames ? '' : 'iteration', subBuilder: IterationUpdate.create)
    ..hasRequiredFields = false
  ;

  @$core.Deprecated(
  'Using this can add significant overhead to your binary. '
  'Use [GeneratedMessageGenericExtensions.deepCopy] instead. '
  'Will be removed in next major version')
  CoreMessage clone() => CoreMessage()..mergeFromMessage(this);
  @$core.Deprecated(
  'Using this can add significant overhead to your binary. '
  'Use [GeneratedMessageGenericExtensions.rebuild] instead. '
  'Will be removed in next major version')
  CoreMessage copyWith(void Function(CoreMessage) updates) => super.copyWith((message) => updates(message as CoreMessage)) as CoreMessage;

  $pb.BuilderInfo get info_ => _i;

  @$core.pragma('dart2js:noInline')
  static CoreMessage create() => CoreMessage._();
  CoreMessage createEmptyInstance() => create();
  static $pb.PbList<CoreMessage> createRepeated() => $pb.PbList<CoreMessage>();
  @$core.pragma('dart2js:noInline')
  static CoreMessage getDefault() => _defaultInstance ??= $pb.GeneratedMessage.$_defaultFor<CoreMessage>(create);
  static CoreMessage? _defaultInstance;

  CoreMessage_Content whichContent() => _CoreMessage_ContentByTag[$_whichOneof(0)]!;
  void clearContent() => clearField($_whichOneof(0));

  @$pb.TagNumber(1)
  $core.String get sessionId => $_getSZ(0);
  @$pb.TagNumber(1)
  set sessionId($core.String v) { $_setString(0, v); }
  @$pb.TagNumber(1)
  $core.bool hasSessionId() => $_has(0);
  @$pb.TagNumber(1)
  void clearSessionId() => clearField(1);

  @$pb.TagNumber(2)
  AgentThinking get thinking => $_getN(1);
  @$pb.TagNumber(2)
  set thinking(AgentThinking v) { setField(2, v); }
  @$pb.TagNumber(2)
  $core.bool hasThinking() => $_has(1);
  @$pb.TagNumber(2)
  void clearThinking() => clearField(2);
  @$pb.TagNumber(2)
  AgentThinking ensureThinking() => $_ensure(1);

  @$pb.TagNumber(3)
  ToolRequest get toolRequest => $_getN(2);
  @$pb.TagNumber(3)
  set toolRequest(ToolRequest v) { setField(3, v); }
  @$pb.TagNumber(3)
  $core.bool hasToolRequest() => $_has(2);
  @$pb.TagNumber(3)
  void clearToolRequest() => clearField(3);
  @$pb.TagNumber(3)
  ToolRequest ensureToolRequest() => $_ensure(2);

  @$pb.TagNumber(4)
  CommandRequest get commandRequest => $_getN(3);
  @$pb.TagNumber(4)
  set commandRequest(CommandRequest v) { setField(4, v); }
  @$pb.TagNumber(4)
  $core.bool hasCommandRequest() => $_has(3);
  @$pb.TagNumber(4)
  void clearCommandRequest() => clearField(4);
  @$pb.TagNumber(4)
  CommandRequest ensureCommandRequest() => $_ensure(3);

  @$pb.TagNumber(5)
  AgentResponse get response => $_getN(4);
  @$pb.TagNumber(5)
  set response(AgentResponse v) { setField(5, v); }
  @$pb.TagNumber(5)
  $core.bool hasResponse() => $_has(4);
  @$pb.TagNumber(5)
  void clearResponse() => clearField(5);
  @$pb.TagNumber(5)
  AgentResponse ensureResponse() => $_ensure(4);

  @$pb.TagNumber(6)
  ErrorMessage get error => $_getN(5);
  @$pb.TagNumber(6)
  set error(ErrorMessage v) { setField(6, v); }
  @$pb.TagNumber(6)
  $core.bool hasError() => $_has(5);
  @$pb.TagNumber(6)
  void clearError() => clearField(6);
  @$pb.TagNumber(6)
  ErrorMessage ensureError() => $_ensure(5);

  @$pb.TagNumber(7)
  IterationUpdate get iteration => $_getN(6);
  @$pb.TagNumber(7)
  set iteration(IterationUpdate v) { setField(7, v); }
  @$pb.TagNumber(7)
  $core.bool hasIteration() => $_has(6);
  @$pb.TagNumber(7)
  void clearIteration() => clearField(7);
  @$pb.TagNumber(7)
  IterationUpdate ensureIteration() => $_ensure(6);
}

class AgentThinking extends $pb.GeneratedMessage {
  factory AgentThinking({
    $core.String? message,
    $core.int? iteration,
  }) {
    final $result = create();
    if (message != null) {
      $result.message = message;
    }
    if (iteration != null) {
      $result.iteration = iteration;
    }
    return $result;
  }
  AgentThinking._() : super();
  factory AgentThinking.fromBuffer($core.List<$core.int> i, [$pb.ExtensionRegistry r = $pb.ExtensionRegistry.EMPTY]) => create()..mergeFromBuffer(i, r);
  factory AgentThinking.fromJson($core.String i, [$pb.ExtensionRegistry r = $pb.ExtensionRegistry.EMPTY]) => create()..mergeFromJson(i, r);

  static final $pb.BuilderInfo _i = $pb.BuilderInfo(_omitMessageNames ? '' : 'AgentThinking', package: const $pb.PackageName(_omitMessageNames ? '' : 'neo.desktop'), createEmptyInstance: create)
    ..aOS(1, _omitFieldNames ? '' : 'message')
    ..a<$core.int>(2, _omitFieldNames ? '' : 'iteration', $pb.PbFieldType.O3)
    ..hasRequiredFields = false
  ;

  @$core.Deprecated(
  'Using this can add significant overhead to your binary. '
  'Use [GeneratedMessageGenericExtensions.deepCopy] instead. '
  'Will be removed in next major version')
  AgentThinking clone() => AgentThinking()..mergeFromMessage(this);
  @$core.Deprecated(
  'Using this can add significant overhead to your binary. '
  'Use [GeneratedMessageGenericExtensions.rebuild] instead. '
  'Will be removed in next major version')
  AgentThinking copyWith(void Function(AgentThinking) updates) => super.copyWith((message) => updates(message as AgentThinking)) as AgentThinking;

  $pb.BuilderInfo get info_ => _i;

  @$core.pragma('dart2js:noInline')
  static AgentThinking create() => AgentThinking._();
  AgentThinking createEmptyInstance() => create();
  static $pb.PbList<AgentThinking> createRepeated() => $pb.PbList<AgentThinking>();
  @$core.pragma('dart2js:noInline')
  static AgentThinking getDefault() => _defaultInstance ??= $pb.GeneratedMessage.$_defaultFor<AgentThinking>(create);
  static AgentThinking? _defaultInstance;

  @$pb.TagNumber(1)
  $core.String get message => $_getSZ(0);
  @$pb.TagNumber(1)
  set message($core.String v) { $_setString(0, v); }
  @$pb.TagNumber(1)
  $core.bool hasMessage() => $_has(0);
  @$pb.TagNumber(1)
  void clearMessage() => clearField(1);

  @$pb.TagNumber(2)
  $core.int get iteration => $_getIZ(1);
  @$pb.TagNumber(2)
  set iteration($core.int v) { $_setSignedInt32(1, v); }
  @$pb.TagNumber(2)
  $core.bool hasIteration() => $_has(1);
  @$pb.TagNumber(2)
  void clearIteration() => clearField(2);
}

class ToolRequest extends $pb.GeneratedMessage {
  factory ToolRequest({
    $core.String? toolCallId,
    $core.String? toolName,
    $core.String? arguments,
    $core.bool? requiresApproval,
  }) {
    final $result = create();
    if (toolCallId != null) {
      $result.toolCallId = toolCallId;
    }
    if (toolName != null) {
      $result.toolName = toolName;
    }
    if (arguments != null) {
      $result.arguments = arguments;
    }
    if (requiresApproval != null) {
      $result.requiresApproval = requiresApproval;
    }
    return $result;
  }
  ToolRequest._() : super();
  factory ToolRequest.fromBuffer($core.List<$core.int> i, [$pb.ExtensionRegistry r = $pb.ExtensionRegistry.EMPTY]) => create()..mergeFromBuffer(i, r);
  factory ToolRequest.fromJson($core.String i, [$pb.ExtensionRegistry r = $pb.ExtensionRegistry.EMPTY]) => create()..mergeFromJson(i, r);

  static final $pb.BuilderInfo _i = $pb.BuilderInfo(_omitMessageNames ? '' : 'ToolRequest', package: const $pb.PackageName(_omitMessageNames ? '' : 'neo.desktop'), createEmptyInstance: create)
    ..aOS(1, _omitFieldNames ? '' : 'toolCallId')
    ..aOS(2, _omitFieldNames ? '' : 'toolName')
    ..aOS(3, _omitFieldNames ? '' : 'arguments')
    ..aOB(4, _omitFieldNames ? '' : 'requiresApproval')
    ..hasRequiredFields = false
  ;

  @$core.Deprecated(
  'Using this can add significant overhead to your binary. '
  'Use [GeneratedMessageGenericExtensions.deepCopy] instead. '
  'Will be removed in next major version')
  ToolRequest clone() => ToolRequest()..mergeFromMessage(this);
  @$core.Deprecated(
  'Using this can add significant overhead to your binary. '
  'Use [GeneratedMessageGenericExtensions.rebuild] instead. '
  'Will be removed in next major version')
  ToolRequest copyWith(void Function(ToolRequest) updates) => super.copyWith((message) => updates(message as ToolRequest)) as ToolRequest;

  $pb.BuilderInfo get info_ => _i;

  @$core.pragma('dart2js:noInline')
  static ToolRequest create() => ToolRequest._();
  ToolRequest createEmptyInstance() => create();
  static $pb.PbList<ToolRequest> createRepeated() => $pb.PbList<ToolRequest>();
  @$core.pragma('dart2js:noInline')
  static ToolRequest getDefault() => _defaultInstance ??= $pb.GeneratedMessage.$_defaultFor<ToolRequest>(create);
  static ToolRequest? _defaultInstance;

  @$pb.TagNumber(1)
  $core.String get toolCallId => $_getSZ(0);
  @$pb.TagNumber(1)
  set toolCallId($core.String v) { $_setString(0, v); }
  @$pb.TagNumber(1)
  $core.bool hasToolCallId() => $_has(0);
  @$pb.TagNumber(1)
  void clearToolCallId() => clearField(1);

  @$pb.TagNumber(2)
  $core.String get toolName => $_getSZ(1);
  @$pb.TagNumber(2)
  set toolName($core.String v) { $_setString(1, v); }
  @$pb.TagNumber(2)
  $core.bool hasToolName() => $_has(1);
  @$pb.TagNumber(2)
  void clearToolName() => clearField(2);

  @$pb.TagNumber(3)
  $core.String get arguments => $_getSZ(2);
  @$pb.TagNumber(3)
  set arguments($core.String v) { $_setString(2, v); }
  @$pb.TagNumber(3)
  $core.bool hasArguments() => $_has(2);
  @$pb.TagNumber(3)
  void clearArguments() => clearField(3);

  @$pb.TagNumber(4)
  $core.bool get requiresApproval => $_getBF(3);
  @$pb.TagNumber(4)
  set requiresApproval($core.bool v) { $_setBool(3, v); }
  @$pb.TagNumber(4)
  $core.bool hasRequiresApproval() => $_has(3);
  @$pb.TagNumber(4)
  void clearRequiresApproval() => clearField(4);
}

class CommandRequest extends $pb.GeneratedMessage {
  factory CommandRequest({
    $core.String? commandId,
    $core.String? command,
    $core.String? explanation,
    $core.bool? isBackground,
  }) {
    final $result = create();
    if (commandId != null) {
      $result.commandId = commandId;
    }
    if (command != null) {
      $result.command = command;
    }
    if (explanation != null) {
      $result.explanation = explanation;
    }
    if (isBackground != null) {
      $result.isBackground = isBackground;
    }
    return $result;
  }
  CommandRequest._() : super();
  factory CommandRequest.fromBuffer($core.List<$core.int> i, [$pb.ExtensionRegistry r = $pb.ExtensionRegistry.EMPTY]) => create()..mergeFromBuffer(i, r);
  factory CommandRequest.fromJson($core.String i, [$pb.ExtensionRegistry r = $pb.ExtensionRegistry.EMPTY]) => create()..mergeFromJson(i, r);

  static final $pb.BuilderInfo _i = $pb.BuilderInfo(_omitMessageNames ? '' : 'CommandRequest', package: const $pb.PackageName(_omitMessageNames ? '' : 'neo.desktop'), createEmptyInstance: create)
    ..aOS(1, _omitFieldNames ? '' : 'commandId')
    ..aOS(2, _omitFieldNames ? '' : 'command')
    ..aOS(3, _omitFieldNames ? '' : 'explanation')
    ..aOB(4, _omitFieldNames ? '' : 'isBackground')
    ..hasRequiredFields = false
  ;

  @$core.Deprecated(
  'Using this can add significant overhead to your binary. '
  'Use [GeneratedMessageGenericExtensions.deepCopy] instead. '
  'Will be removed in next major version')
  CommandRequest clone() => CommandRequest()..mergeFromMessage(this);
  @$core.Deprecated(
  'Using this can add significant overhead to your binary. '
  'Use [GeneratedMessageGenericExtensions.rebuild] instead. '
  'Will be removed in next major version')
  CommandRequest copyWith(void Function(CommandRequest) updates) => super.copyWith((message) => updates(message as CommandRequest)) as CommandRequest;

  $pb.BuilderInfo get info_ => _i;

  @$core.pragma('dart2js:noInline')
  static CommandRequest create() => CommandRequest._();
  CommandRequest createEmptyInstance() => create();
  static $pb.PbList<CommandRequest> createRepeated() => $pb.PbList<CommandRequest>();
  @$core.pragma('dart2js:noInline')
  static CommandRequest getDefault() => _defaultInstance ??= $pb.GeneratedMessage.$_defaultFor<CommandRequest>(create);
  static CommandRequest? _defaultInstance;

  @$pb.TagNumber(1)
  $core.String get commandId => $_getSZ(0);
  @$pb.TagNumber(1)
  set commandId($core.String v) { $_setString(0, v); }
  @$pb.TagNumber(1)
  $core.bool hasCommandId() => $_has(0);
  @$pb.TagNumber(1)
  void clearCommandId() => clearField(1);

  @$pb.TagNumber(2)
  $core.String get command => $_getSZ(1);
  @$pb.TagNumber(2)
  set command($core.String v) { $_setString(1, v); }
  @$pb.TagNumber(2)
  $core.bool hasCommand() => $_has(1);
  @$pb.TagNumber(2)
  void clearCommand() => clearField(2);

  @$pb.TagNumber(3)
  $core.String get explanation => $_getSZ(2);
  @$pb.TagNumber(3)
  set explanation($core.String v) { $_setString(2, v); }
  @$pb.TagNumber(3)
  $core.bool hasExplanation() => $_has(2);
  @$pb.TagNumber(3)
  void clearExplanation() => clearField(3);

  @$pb.TagNumber(4)
  $core.bool get isBackground => $_getBF(3);
  @$pb.TagNumber(4)
  set isBackground($core.bool v) { $_setBool(3, v); }
  @$pb.TagNumber(4)
  $core.bool hasIsBackground() => $_has(3);
  @$pb.TagNumber(4)
  void clearIsBackground() => clearField(4);
}

class AgentResponse extends $pb.GeneratedMessage {
  factory AgentResponse({
    $core.String? message,
    $core.bool? isFinal,
    $core.int? totalIterations,
  }) {
    final $result = create();
    if (message != null) {
      $result.message = message;
    }
    if (isFinal != null) {
      $result.isFinal = isFinal;
    }
    if (totalIterations != null) {
      $result.totalIterations = totalIterations;
    }
    return $result;
  }
  AgentResponse._() : super();
  factory AgentResponse.fromBuffer($core.List<$core.int> i, [$pb.ExtensionRegistry r = $pb.ExtensionRegistry.EMPTY]) => create()..mergeFromBuffer(i, r);
  factory AgentResponse.fromJson($core.String i, [$pb.ExtensionRegistry r = $pb.ExtensionRegistry.EMPTY]) => create()..mergeFromJson(i, r);

  static final $pb.BuilderInfo _i = $pb.BuilderInfo(_omitMessageNames ? '' : 'AgentResponse', package: const $pb.PackageName(_omitMessageNames ? '' : 'neo.desktop'), createEmptyInstance: create)
    ..aOS(1, _omitFieldNames ? '' : 'message')
    ..aOB(2, _omitFieldNames ? '' : 'isFinal')
    ..a<$core.int>(3, _omitFieldNames ? '' : 'totalIterations', $pb.PbFieldType.O3)
    ..hasRequiredFields = false
  ;

  @$core.Deprecated(
  'Using this can add significant overhead to your binary. '
  'Use [GeneratedMessageGenericExtensions.deepCopy] instead. '
  'Will be removed in next major version')
  AgentResponse clone() => AgentResponse()..mergeFromMessage(this);
  @$core.Deprecated(
  'Using this can add significant overhead to your binary. '
  'Use [GeneratedMessageGenericExtensions.rebuild] instead. '
  'Will be removed in next major version')
  AgentResponse copyWith(void Function(AgentResponse) updates) => super.copyWith((message) => updates(message as AgentResponse)) as AgentResponse;

  $pb.BuilderInfo get info_ => _i;

  @$core.pragma('dart2js:noInline')
  static AgentResponse create() => AgentResponse._();
  AgentResponse createEmptyInstance() => create();
  static $pb.PbList<AgentResponse> createRepeated() => $pb.PbList<AgentResponse>();
  @$core.pragma('dart2js:noInline')
  static AgentResponse getDefault() => _defaultInstance ??= $pb.GeneratedMessage.$_defaultFor<AgentResponse>(create);
  static AgentResponse? _defaultInstance;

  @$pb.TagNumber(1)
  $core.String get message => $_getSZ(0);
  @$pb.TagNumber(1)
  set message($core.String v) { $_setString(0, v); }
  @$pb.TagNumber(1)
  $core.bool hasMessage() => $_has(0);
  @$pb.TagNumber(1)
  void clearMessage() => clearField(1);

  @$pb.TagNumber(2)
  $core.bool get isFinal => $_getBF(1);
  @$pb.TagNumber(2)
  set isFinal($core.bool v) { $_setBool(1, v); }
  @$pb.TagNumber(2)
  $core.bool hasIsFinal() => $_has(1);
  @$pb.TagNumber(2)
  void clearIsFinal() => clearField(2);

  @$pb.TagNumber(3)
  $core.int get totalIterations => $_getIZ(2);
  @$pb.TagNumber(3)
  set totalIterations($core.int v) { $_setSignedInt32(2, v); }
  @$pb.TagNumber(3)
  $core.bool hasTotalIterations() => $_has(2);
  @$pb.TagNumber(3)
  void clearTotalIterations() => clearField(3);
}

class ErrorMessage extends $pb.GeneratedMessage {
  factory ErrorMessage({
    $core.String? error,
    $core.String? details,
  }) {
    final $result = create();
    if (error != null) {
      $result.error = error;
    }
    if (details != null) {
      $result.details = details;
    }
    return $result;
  }
  ErrorMessage._() : super();
  factory ErrorMessage.fromBuffer($core.List<$core.int> i, [$pb.ExtensionRegistry r = $pb.ExtensionRegistry.EMPTY]) => create()..mergeFromBuffer(i, r);
  factory ErrorMessage.fromJson($core.String i, [$pb.ExtensionRegistry r = $pb.ExtensionRegistry.EMPTY]) => create()..mergeFromJson(i, r);

  static final $pb.BuilderInfo _i = $pb.BuilderInfo(_omitMessageNames ? '' : 'ErrorMessage', package: const $pb.PackageName(_omitMessageNames ? '' : 'neo.desktop'), createEmptyInstance: create)
    ..aOS(1, _omitFieldNames ? '' : 'error')
    ..aOS(2, _omitFieldNames ? '' : 'details')
    ..hasRequiredFields = false
  ;

  @$core.Deprecated(
  'Using this can add significant overhead to your binary. '
  'Use [GeneratedMessageGenericExtensions.deepCopy] instead. '
  'Will be removed in next major version')
  ErrorMessage clone() => ErrorMessage()..mergeFromMessage(this);
  @$core.Deprecated(
  'Using this can add significant overhead to your binary. '
  'Use [GeneratedMessageGenericExtensions.rebuild] instead. '
  'Will be removed in next major version')
  ErrorMessage copyWith(void Function(ErrorMessage) updates) => super.copyWith((message) => updates(message as ErrorMessage)) as ErrorMessage;

  $pb.BuilderInfo get info_ => _i;

  @$core.pragma('dart2js:noInline')
  static ErrorMessage create() => ErrorMessage._();
  ErrorMessage createEmptyInstance() => create();
  static $pb.PbList<ErrorMessage> createRepeated() => $pb.PbList<ErrorMessage>();
  @$core.pragma('dart2js:noInline')
  static ErrorMessage getDefault() => _defaultInstance ??= $pb.GeneratedMessage.$_defaultFor<ErrorMessage>(create);
  static ErrorMessage? _defaultInstance;

  @$pb.TagNumber(1)
  $core.String get error => $_getSZ(0);
  @$pb.TagNumber(1)
  set error($core.String v) { $_setString(0, v); }
  @$pb.TagNumber(1)
  $core.bool hasError() => $_has(0);
  @$pb.TagNumber(1)
  void clearError() => clearField(1);

  @$pb.TagNumber(2)
  $core.String get details => $_getSZ(1);
  @$pb.TagNumber(2)
  set details($core.String v) { $_setString(1, v); }
  @$pb.TagNumber(2)
  $core.bool hasDetails() => $_has(1);
  @$pb.TagNumber(2)
  void clearDetails() => clearField(2);
}

class IterationUpdate extends $pb.GeneratedMessage {
  factory IterationUpdate({
    $core.int? current,
    $core.int? max,
  }) {
    final $result = create();
    if (current != null) {
      $result.current = current;
    }
    if (max != null) {
      $result.max = max;
    }
    return $result;
  }
  IterationUpdate._() : super();
  factory IterationUpdate.fromBuffer($core.List<$core.int> i, [$pb.ExtensionRegistry r = $pb.ExtensionRegistry.EMPTY]) => create()..mergeFromBuffer(i, r);
  factory IterationUpdate.fromJson($core.String i, [$pb.ExtensionRegistry r = $pb.ExtensionRegistry.EMPTY]) => create()..mergeFromJson(i, r);

  static final $pb.BuilderInfo _i = $pb.BuilderInfo(_omitMessageNames ? '' : 'IterationUpdate', package: const $pb.PackageName(_omitMessageNames ? '' : 'neo.desktop'), createEmptyInstance: create)
    ..a<$core.int>(1, _omitFieldNames ? '' : 'current', $pb.PbFieldType.O3)
    ..a<$core.int>(2, _omitFieldNames ? '' : 'max', $pb.PbFieldType.O3)
    ..hasRequiredFields = false
  ;

  @$core.Deprecated(
  'Using this can add significant overhead to your binary. '
  'Use [GeneratedMessageGenericExtensions.deepCopy] instead. '
  'Will be removed in next major version')
  IterationUpdate clone() => IterationUpdate()..mergeFromMessage(this);
  @$core.Deprecated(
  'Using this can add significant overhead to your binary. '
  'Use [GeneratedMessageGenericExtensions.rebuild] instead. '
  'Will be removed in next major version')
  IterationUpdate copyWith(void Function(IterationUpdate) updates) => super.copyWith((message) => updates(message as IterationUpdate)) as IterationUpdate;

  $pb.BuilderInfo get info_ => _i;

  @$core.pragma('dart2js:noInline')
  static IterationUpdate create() => IterationUpdate._();
  IterationUpdate createEmptyInstance() => create();
  static $pb.PbList<IterationUpdate> createRepeated() => $pb.PbList<IterationUpdate>();
  @$core.pragma('dart2js:noInline')
  static IterationUpdate getDefault() => _defaultInstance ??= $pb.GeneratedMessage.$_defaultFor<IterationUpdate>(create);
  static IterationUpdate? _defaultInstance;

  @$pb.TagNumber(1)
  $core.int get current => $_getIZ(0);
  @$pb.TagNumber(1)
  set current($core.int v) { $_setSignedInt32(0, v); }
  @$pb.TagNumber(1)
  $core.bool hasCurrent() => $_has(0);
  @$pb.TagNumber(1)
  void clearCurrent() => clearField(1);

  @$pb.TagNumber(2)
  $core.int get max => $_getIZ(1);
  @$pb.TagNumber(2)
  set max($core.int v) { $_setSignedInt32(1, v); }
  @$pb.TagNumber(2)
  $core.bool hasMax() => $_has(1);
  @$pb.TagNumber(2)
  void clearMax() => clearField(2);
}

/// Health check
class HealthCheckRequest extends $pb.GeneratedMessage {
  factory HealthCheckRequest() => create();
  HealthCheckRequest._() : super();
  factory HealthCheckRequest.fromBuffer($core.List<$core.int> i, [$pb.ExtensionRegistry r = $pb.ExtensionRegistry.EMPTY]) => create()..mergeFromBuffer(i, r);
  factory HealthCheckRequest.fromJson($core.String i, [$pb.ExtensionRegistry r = $pb.ExtensionRegistry.EMPTY]) => create()..mergeFromJson(i, r);

  static final $pb.BuilderInfo _i = $pb.BuilderInfo(_omitMessageNames ? '' : 'HealthCheckRequest', package: const $pb.PackageName(_omitMessageNames ? '' : 'neo.desktop'), createEmptyInstance: create)
    ..hasRequiredFields = false
  ;

  @$core.Deprecated(
  'Using this can add significant overhead to your binary. '
  'Use [GeneratedMessageGenericExtensions.deepCopy] instead. '
  'Will be removed in next major version')
  HealthCheckRequest clone() => HealthCheckRequest()..mergeFromMessage(this);
  @$core.Deprecated(
  'Using this can add significant overhead to your binary. '
  'Use [GeneratedMessageGenericExtensions.rebuild] instead. '
  'Will be removed in next major version')
  HealthCheckRequest copyWith(void Function(HealthCheckRequest) updates) => super.copyWith((message) => updates(message as HealthCheckRequest)) as HealthCheckRequest;

  $pb.BuilderInfo get info_ => _i;

  @$core.pragma('dart2js:noInline')
  static HealthCheckRequest create() => HealthCheckRequest._();
  HealthCheckRequest createEmptyInstance() => create();
  static $pb.PbList<HealthCheckRequest> createRepeated() => $pb.PbList<HealthCheckRequest>();
  @$core.pragma('dart2js:noInline')
  static HealthCheckRequest getDefault() => _defaultInstance ??= $pb.GeneratedMessage.$_defaultFor<HealthCheckRequest>(create);
  static HealthCheckRequest? _defaultInstance;
}

class HealthCheckResponse extends $pb.GeneratedMessage {
  factory HealthCheckResponse({
    $core.bool? healthy,
    $core.String? version,
    $core.Map<$core.String, $core.String>? status,
  }) {
    final $result = create();
    if (healthy != null) {
      $result.healthy = healthy;
    }
    if (version != null) {
      $result.version = version;
    }
    if (status != null) {
      $result.status.addAll(status);
    }
    return $result;
  }
  HealthCheckResponse._() : super();
  factory HealthCheckResponse.fromBuffer($core.List<$core.int> i, [$pb.ExtensionRegistry r = $pb.ExtensionRegistry.EMPTY]) => create()..mergeFromBuffer(i, r);
  factory HealthCheckResponse.fromJson($core.String i, [$pb.ExtensionRegistry r = $pb.ExtensionRegistry.EMPTY]) => create()..mergeFromJson(i, r);

  static final $pb.BuilderInfo _i = $pb.BuilderInfo(_omitMessageNames ? '' : 'HealthCheckResponse', package: const $pb.PackageName(_omitMessageNames ? '' : 'neo.desktop'), createEmptyInstance: create)
    ..aOB(1, _omitFieldNames ? '' : 'healthy')
    ..aOS(2, _omitFieldNames ? '' : 'version')
    ..m<$core.String, $core.String>(3, _omitFieldNames ? '' : 'status', entryClassName: 'HealthCheckResponse.StatusEntry', keyFieldType: $pb.PbFieldType.OS, valueFieldType: $pb.PbFieldType.OS, packageName: const $pb.PackageName('neo.desktop'))
    ..hasRequiredFields = false
  ;

  @$core.Deprecated(
  'Using this can add significant overhead to your binary. '
  'Use [GeneratedMessageGenericExtensions.deepCopy] instead. '
  'Will be removed in next major version')
  HealthCheckResponse clone() => HealthCheckResponse()..mergeFromMessage(this);
  @$core.Deprecated(
  'Using this can add significant overhead to your binary. '
  'Use [GeneratedMessageGenericExtensions.rebuild] instead. '
  'Will be removed in next major version')
  HealthCheckResponse copyWith(void Function(HealthCheckResponse) updates) => super.copyWith((message) => updates(message as HealthCheckResponse)) as HealthCheckResponse;

  $pb.BuilderInfo get info_ => _i;

  @$core.pragma('dart2js:noInline')
  static HealthCheckResponse create() => HealthCheckResponse._();
  HealthCheckResponse createEmptyInstance() => create();
  static $pb.PbList<HealthCheckResponse> createRepeated() => $pb.PbList<HealthCheckResponse>();
  @$core.pragma('dart2js:noInline')
  static HealthCheckResponse getDefault() => _defaultInstance ??= $pb.GeneratedMessage.$_defaultFor<HealthCheckResponse>(create);
  static HealthCheckResponse? _defaultInstance;

  @$pb.TagNumber(1)
  $core.bool get healthy => $_getBF(0);
  @$pb.TagNumber(1)
  set healthy($core.bool v) { $_setBool(0, v); }
  @$pb.TagNumber(1)
  $core.bool hasHealthy() => $_has(0);
  @$pb.TagNumber(1)
  void clearHealthy() => clearField(1);

  @$pb.TagNumber(2)
  $core.String get version => $_getSZ(1);
  @$pb.TagNumber(2)
  set version($core.String v) { $_setString(1, v); }
  @$pb.TagNumber(2)
  $core.bool hasVersion() => $_has(1);
  @$pb.TagNumber(2)
  void clearVersion() => clearField(2);

  @$pb.TagNumber(3)
  $core.Map<$core.String, $core.String> get status => $_getMap(2);
}


const _omitFieldNames = $core.bool.fromEnvironment('protobuf.omit_field_names');
const _omitMessageNames = $core.bool.fromEnvironment('protobuf.omit_message_names');
