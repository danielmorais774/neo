import 'package:flutter/material.dart';
import 'package:google_fonts/google_fonts.dart';

class NeoColors {
  static const bgPrimary = Color(0xFF0a0e1a);
  static const bgSecondary = Color(0xFF111827);
  static const bgTertiary = Color(0xFF1e293b);
  static const accent = Color(0xFF3b82f6);
  static const accentHover = Color(0xFF2563eb);
  static const textPrimary = Color(0xFFe6edf3);
  static const textSecondary = Color(0xFF7d8590);
  static const borderColor = Color(0x1F3B82F6);
  static const userBubbleStart = Color(0xFF06b6d4);
  static const userBubbleEnd = Color(0xFF0891b2);
  static const success = Color(0xFF22c55e);
  static const warning = Color(0xFFf59e0b);
  static const error = Color(0xFFef4444);
  static const thinkingBg = Color(0xFF1a1f2e);
}

class NeoTheme {
  static ThemeData get dark {
    final base = ThemeData.dark();
    return base.copyWith(
      scaffoldBackgroundColor: NeoColors.bgPrimary,
      colorScheme: const ColorScheme.dark(
        primary: NeoColors.accent,
        secondary: NeoColors.userBubbleStart,
        surface: NeoColors.bgSecondary,
        error: NeoColors.error,
        onPrimary: Colors.white,
        onSecondary: Colors.white,
        onSurface: NeoColors.textPrimary,
        onError: Colors.white,
      ),
      appBarTheme: AppBarTheme(
        backgroundColor: NeoColors.bgSecondary,
        foregroundColor: NeoColors.textPrimary,
        elevation: 0,
        titleTextStyle: GoogleFonts.inter(
          fontSize: 18,
          fontWeight: FontWeight.w600,
          color: NeoColors.textPrimary,
        ),
      ),
      drawerTheme: const DrawerThemeData(
        backgroundColor: NeoColors.bgSecondary,
      ),
      cardTheme: const CardThemeData(
        color: NeoColors.bgTertiary,
        elevation: 0,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.all(Radius.circular(12)),
          side: BorderSide(color: NeoColors.borderColor),
        ),
      ),
      inputDecorationTheme: InputDecorationTheme(
        filled: true,
        fillColor: NeoColors.bgTertiary,
        border: OutlineInputBorder(
          borderRadius: BorderRadius.circular(12),
          borderSide: const BorderSide(color: NeoColors.borderColor),
        ),
        enabledBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(12),
          borderSide: const BorderSide(color: NeoColors.borderColor),
        ),
        focusedBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(12),
          borderSide: const BorderSide(color: NeoColors.accent, width: 1.5),
        ),
        hintStyle: const TextStyle(color: NeoColors.textSecondary),
        contentPadding: const EdgeInsets.symmetric(horizontal: 16, vertical: 14),
      ),
      elevatedButtonTheme: ElevatedButtonThemeData(
        style: ElevatedButton.styleFrom(
          backgroundColor: NeoColors.accent,
          foregroundColor: Colors.white,
          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(10)),
          padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 14),
        ),
      ),
      textTheme: GoogleFonts.interTextTheme(base.textTheme).apply(
        bodyColor: NeoColors.textPrimary,
        displayColor: NeoColors.textPrimary,
      ),
      dividerColor: NeoColors.borderColor,
      iconTheme: const IconThemeData(color: NeoColors.textSecondary),
    );
  }
}
