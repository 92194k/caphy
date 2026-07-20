import 'dart:math' as math;
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

/// Safely coerce a JSON value (int, double, or string) to an int.
int asInt(dynamic v, [int fallback = 0]) {
  if (v is int) return v;
  if (v is num) return v.toInt();
  return int.tryParse('${v ?? ''}') ?? fallback;
}

/// The CAPHY logo — a gradient teal tile with a living camera-eye that blinks
/// and glances side to side, with a "C" on the pupil.
class CaphyLogo extends StatefulWidget {
  final double size;
  final bool animate;
  const CaphyLogo({super.key, this.size = 48, this.animate = true});
  @override
  State<CaphyLogo> createState() => _CaphyLogoState();
}

class _CaphyLogoState extends State<CaphyLogo>
    with SingleTickerProviderStateMixin {
  AnimationController? _c;

  @override
  void initState() {
    super.initState();
    if (widget.animate) {
      _c = AnimationController(
          vsync: this, duration: const Duration(milliseconds: 4200))
        ..repeat();
    }
  }

  @override
  void dispose() {
    _c?.dispose();
    super.dispose();
  }

  Widget _tile(double lid, double dx) {
    final s = widget.size;
    return Container(
      width: s,
      height: s,
      decoration: BoxDecoration(
        gradient: const LinearGradient(
            begin: Alignment.topLeft,
            end: Alignment.bottomRight,
            colors: [Color(0xFF2A9D8F), Color(0xFF217C72)]),
        borderRadius: BorderRadius.circular(s * 0.24),
      ),
      child: ClipRRect(
        borderRadius: BorderRadius.circular(s * 0.24),
        child: Center(
          child: SizedBox(
            width: s * 0.56,
            height: s * 0.34,
            child: Stack(alignment: Alignment.center, children: [
              // almond eye (its height shrinks to blink)
              Container(
                width: s * 0.56,
                height: (s * 0.34) * lid,
                decoration: BoxDecoration(
                  color: const Color(0xFF08110F),
                  borderRadius: BorderRadius.circular(s * 0.17),
                ),
              ),
              // round pupil (glances side to side, hides during a blink)
              Transform.translate(
                offset: Offset(dx, 0),
                child: Opacity(
                  opacity: lid > 0.4 ? 1 : 0,
                  child: Container(
                    width: s * 0.20,
                    height: s * 0.20,
                    decoration: const BoxDecoration(
                        color: cTeal2, shape: BoxShape.circle),
                  ),
                ),
              ),
            ]),
          ),
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    if (_c == null) return _tile(1.0, 0.0);
    return AnimatedBuilder(
      animation: _c!,
      builder: (_, _) {
        // phase from the shared wall clock, so the eye continues seamlessly
        // across screens instead of restarting each time the logo mounts.
        final t = (DateTime.now().millisecondsSinceEpoch % 4200) / 4200.0;
        final dx = math.sin(t * 2 * math.pi) * widget.size * 0.10; // glance
        double lid = 1.0;
        final d = (t - 0.9).abs(); // one blink per loop
        if (d < 0.035) lid = 0.08 + (d / 0.035) * 0.92;
        return _tile(lid, dx);
      },
    );
  }
}

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
