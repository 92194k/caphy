import 'dart:math' as math;
import 'package:flutter/material.dart';

// ---- palette (matches the CAPHY web theme) ----
// Near-black base, indigo glass cards, blue->purple gradient accents.
// cTeal keeps its name but is now the blue; cTeal2 is the violet highlight.
const cBg = Color(0xFF08080F);        // near-black base
const cPanel = Color(0xFF12121F);     // indigo card
const cPanel2 = Color(0xFF161528);    // indigo card (raised)
const cLine = Color(0xFF2F2F4D);
const cText = Color(0xFFECEAFB);
const cMuted = Color(0xFF9A97BD);
const cDim = Color(0xFF66638A);
const cTeal = Color(0xFF5B7BFF);      // blue (primary accent)
const cTeal2 = Color(0xFFA78BFA);     // violet (highlight)
const cBlue = Color(0xFF4A7FFF);
const cPurple = Color(0xFF8B6FFF);
const cElectric = Color(0xFF5B7BFF);
const cElectric2 = Color(0xFFA78BFA);
const cOrange = Color(0xFFE0A44C);
const cRed = Color(0xFFF0596B);
const cGreen = Color(0xFF4FD1A0);

/// The signature blue->purple gradient used across the app.
const cGrad = LinearGradient(
    begin: Alignment.topLeft,
    end: Alignment.bottomRight,
    colors: [Color(0xFF4A7FFF), Color(0xFF7B6BFF), Color(0xFFA78BFA)]);

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
            colors: [Color(0xFF5B7BFF), Color(0xFF8B6FFF), Color(0xFFA78BFA)]),
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
                  color: const Color(0xFF0A1024),
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

/// A larger, tappable CAPHY logo for the login screen — sits inside a soft
/// pulsing glow ring, gently floats, and bounces when tapped.
class InteractiveLogo extends StatefulWidget {
  @override
  State<InteractiveLogo> createState() => InteractiveLogoState();
}

class InteractiveLogoState extends State<InteractiveLogo>
    with TickerProviderStateMixin {
  late final AnimationController _float = AnimationController(
      vsync: this, duration: const Duration(seconds: 5))
    ..repeat(reverse: true);
  late final AnimationController _bounce = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 480),
      lowerBound: 0,
      upperBound: 1);

  @override
  void dispose() {
    _float.dispose();
    _bounce.dispose();
    super.dispose();
  }

  void _tap() {
    _bounce.forward(from: 0);
  }

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: _tap,
      child: AnimatedBuilder(
        animation: Listenable.merge([_float, _bounce]),
        builder: (_, child) {
          final floatY = math.sin(_float.value * math.pi) * -8;
          // playful bounce: quick scale up then settle
          final b = _bounce.value;
          final scale =
              b == 0 ? 1.0 : 1.0 + math.sin(b * math.pi) * 0.18 * (1 - b * 0.3);
          final glow = 0.35 + math.sin(_float.value * math.pi) * 0.25;
          return Transform.translate(
            offset: Offset(0, floatY),
            child: Transform.scale(
              scale: scale,
              child: Container(
                padding: const EdgeInsets.all(14),
                decoration: BoxDecoration(
                  shape: BoxShape.circle,
                  gradient: RadialGradient(colors: [
                    cPurple.withValues(alpha: 0.22 * glow),
                    Colors.transparent,
                  ]),
                  boxShadow: [
                    BoxShadow(
                        color: cTeal.withValues(alpha: glow * 0.5),
                        blurRadius: 36,
                        spreadRadius: 2),
                  ],
                ),
                child: child,
              ),
            ),
          );
        },
        child: const CaphyLogo(size: 104),
      ),
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

/// A slate-blue glass panel container (premium look: translucent fill,
/// hairline highlight border, soft depth shadow).
Widget panel({required Widget child, EdgeInsets? padding}) => Container(
      width: double.infinity,
      padding: padding ?? const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: cPanel2.withValues(alpha: 0.72),
        borderRadius: BorderRadius.circular(16),
        border: Border.all(color: const Color(0x22789AD2)),
        boxShadow: const [
          BoxShadow(
              color: Color(0x33030712),
              blurRadius: 24,
              offset: Offset(0, 8)),
        ],
      ),
      child: child,
    );
