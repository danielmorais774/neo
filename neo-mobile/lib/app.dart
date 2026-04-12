import 'package:flutter/material.dart';

import 'config/theme.dart';
import 'screens/home_screen.dart';

class NeoApp extends StatelessWidget {
  const NeoApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Neo Mobile',
      theme: NeoTheme.dark,
      debugShowCheckedModeBanner: false,
      home: const HomeScreen(),
    );
  }
}
