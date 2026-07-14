import 'package:flutter/material.dart';

// ---- palette (matches the CAPHY web console) ----
const cBg = Color(0xFF0B1117);
const cPanel = Color(0xFF16232E);
const cPanel2 = Color(0xFF1A2A36);
const cLine = Color(0xFF26404F);
const cText = Color(0xFFE6EDF3);
const cMuted = Color(0xFF8AA0B0);
const cDim = Color(0xFF5C7180);
const cTeal = Color(0xFF2A9D8F);
const cTeal2 = Color(0xFF3DD7C4);
const cOrange = Color(0xFFF4A261);
const cRed = Color(0xFFE5484D);
const cGreen = Color(0xFF3FB950);

Color tierColor(int t) => t >= 3 ? cRed : (t == 2 ? cOrange : cTeal2);

/// Small rounded colored tier badge, e.g. "Tier 2".
Widget tierPill(int t) => Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
      decoration: BoxDecoration(
        color: tierColor(t).withValues(alpha: 0.15),
        borderRadius: BorderRadius.circular(20),
        border: Border.all(color: tierColor(t).withValues(alpha: 0.6)),
      ),
      child: Text('Tier $t',
          style: TextStyle(
              color: tierColor(t), fontSize: 12, fontWeight: FontWeight.bold)),
    );

/// A bordered dark panel container.
Widget panel({required Widget child, EdgeInsets? padding}) => Container(
      width: double.infinity,
      padding: padding ?? const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: cPanel2,
        borderRadius: BorderRadius.circular(14),
        border: Border.all(color: cLine),
      ),
      child: child,
    );
