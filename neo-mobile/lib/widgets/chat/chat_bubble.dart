import 'package:flutter/material.dart';
import 'package:flutter_markdown/flutter_markdown.dart';

import '../../config/theme.dart';
import '../../models/chat_message.dart';

class ChatBubble extends StatelessWidget {
  final ChatMessage message;

  const ChatBubble({super.key, required this.message});

  @override
  Widget build(BuildContext context) {
    if (message.isThinking) return _buildThinking(context);
    if (message.isError) return _buildError(context);
    if (message.role == MessageRole.system) return _buildSystem(context);

    final isUser = message.isUser;
    return Align(
      alignment: isUser ? Alignment.centerRight : Alignment.centerLeft,
      child: Container(
        constraints: BoxConstraints(
          maxWidth: MediaQuery.of(context).size.width * 0.82,
        ),
        margin: EdgeInsets.only(
          left: isUser ? 48 : 8,
          right: isUser ? 8 : 48,
          top: 4,
          bottom: 4,
        ),
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
        decoration: BoxDecoration(
          gradient: isUser
              ? const LinearGradient(
                  colors: [NeoColors.userBubbleStart, NeoColors.userBubbleEnd],
                  begin: Alignment.topLeft,
                  end: Alignment.bottomRight,
                )
              : null,
          color: isUser ? null : NeoColors.bgTertiary,
          borderRadius: BorderRadius.only(
            topLeft: const Radius.circular(16),
            topRight: const Radius.circular(16),
            bottomLeft: Radius.circular(isUser ? 16 : 4),
            bottomRight: Radius.circular(isUser ? 4 : 16),
          ),
          border: isUser ? null : Border.all(color: NeoColors.borderColor),
        ),
        child: isUser
            ? Text(
                message.content,
                style: const TextStyle(color: Colors.white, fontSize: 15),
              )
            : MarkdownBody(
                data: message.content,
                styleSheet: MarkdownStyleSheet(
                  p: const TextStyle(color: NeoColors.textPrimary, fontSize: 15),
                  code: TextStyle(
                    color: NeoColors.accent,
                    backgroundColor: NeoColors.bgPrimary.withAlpha(128),
                    fontSize: 13,
                  ),
                  codeblockDecoration: BoxDecoration(
                    color: NeoColors.bgPrimary,
                    borderRadius: BorderRadius.circular(8),
                    border: Border.all(color: NeoColors.borderColor),
                  ),
                  h1: const TextStyle(color: NeoColors.textPrimary, fontSize: 20),
                  h2: const TextStyle(color: NeoColors.textPrimary, fontSize: 18),
                  h3: const TextStyle(color: NeoColors.textPrimary, fontSize: 16),
                  listBullet: const TextStyle(color: NeoColors.textSecondary),
                  blockquoteDecoration: BoxDecoration(
                    color: NeoColors.bgPrimary.withAlpha(128),
                    border: const Border(
                      left: BorderSide(color: NeoColors.accent, width: 3),
                    ),
                  ),
                ),
                selectable: true,
              ),
      ),
    );
  }

  Widget _buildThinking(BuildContext context) {
    return Container(
      margin: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
      decoration: BoxDecoration(
        color: NeoColors.thinkingBg,
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: NeoColors.accent.withAlpha(51)),
      ),
      child: Row(
        children: [
          SizedBox(
            width: 16,
            height: 16,
            child: CircularProgressIndicator(
              strokeWidth: 2,
              color: NeoColors.accent.withAlpha(179),
            ),
          ),
          const SizedBox(width: 10),
          Expanded(
            child: Text(
              message.content,
              style: TextStyle(
                color: NeoColors.textSecondary.withAlpha(204),
                fontStyle: FontStyle.italic,
                fontSize: 13,
              ),
            ),
          ),
          if (message.iteration != null)
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
              decoration: BoxDecoration(
                color: NeoColors.accent.withAlpha(31),
                borderRadius: BorderRadius.circular(10),
              ),
              child: Text(
                '#${message.iteration}',
                style: const TextStyle(color: NeoColors.accent, fontSize: 11),
              ),
            ),
        ],
      ),
    );
  }

  Widget _buildError(BuildContext context) {
    return Container(
      margin: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
      decoration: BoxDecoration(
        color: NeoColors.error.withAlpha(26),
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: NeoColors.error.withAlpha(77)),
      ),
      child: Row(
        children: [
          const Icon(Icons.error_outline, color: NeoColors.error, size: 18),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              message.content,
              style: const TextStyle(color: NeoColors.error, fontSize: 13),
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildSystem(BuildContext context) {
    return Container(
      margin: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 8),
      decoration: BoxDecoration(
        color: NeoColors.warning.withAlpha(20),
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: NeoColors.warning.withAlpha(51)),
      ),
      child: Text(
        message.content,
        style: const TextStyle(color: NeoColors.textSecondary, fontSize: 13),
      ),
    );
  }
}
