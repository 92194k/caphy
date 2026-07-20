// Smoke test for the CAPHY app shell.
//
// The previous version of this file was still the default Flutter counter-app
// template and referenced a `MyApp` class that doesn't exist in this project
// (the real root widget is `CaphyApp`), so `flutter analyze`/`flutter test`
// failed outright. This replaces it with a minimal test that actually
// exercises the real app: it boots `CaphyApp` with a mocked, empty
// SharedPreferences store (so there's no saved login token) and checks that
// it lands on the login screen.

import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:caphy_app/api.dart';
import 'package:caphy_app/main.dart';

void main() {
  testWidgets('CaphyApp shows the login screen when logged out',
      (WidgetTester tester) async {
    // Store.init() reads SharedPreferences; mock it so the test doesn't
    // touch platform channels or a real device.
    SharedPreferences.setMockInitialValues({});
    await Store.init();

    await tester.pumpWidget(const CaphyApp());
    await tester.pump();

    // No saved token -> LoginScreen, which always shows the CAPHY branding.
    expect(find.text('CAPHY'), findsOneWidget);
    expect(find.text('AI SECURITY'), findsOneWidget);
  });
}
