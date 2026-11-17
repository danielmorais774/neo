import 'package:flutter/material.dart';

import '../../config/theme.dart';

class ErrorBanner extends StatelessWidget {
  final String message;
  final VoidCallback? onDismiss;

  const ErrorBanner({super.key, required this.message, this.onDismiss});

  @override
  Widget build(BuildContext context) {
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
      decoration: BoxDecoration(
        color: NeoColors.error.withAlpha(26),
        border: const Border(bottom: BorderSide(color: NeoColors.error)),
      ),
      child: Row(
        children: [
          const Icon(Icons.warning_amber_rounded, color: NeoColors.error, size: 18),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              message,
              style: const TextStyle(color: NeoColors.error, fontSize: 13),
            ),
          ),
          if (onDismiss != null)
            GestureDetector(
              onTap: onDismiss,
              child: const Icon(Icons.close, color: NeoColors.error, size: 18),
            ),
        ],
      ),
    );
  }
}
