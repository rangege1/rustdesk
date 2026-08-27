import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_hbb/common.dart';

class CustomerStatusBadge extends StatelessWidget {
  const CustomerStatusBadge();

  @override
  Widget build(BuildContext context) {
    return const Row(mainAxisSize: MainAxisSize.min, children: [
      Icon(Icons.circle, color: Color(0xFF12B76A), size: 9),
      SizedBox(width: 6),
      Text('服务运行中', style: TextStyle(color: Color(0xFF157A4E), fontSize: 13)),
    ]);
  }
}

class CustomerCredential extends StatelessWidget {
  const CustomerCredential({
    required this.label,
    required this.value,
    required this.icon,
  });

  final String label;
  final String value;
  final IconData icon;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.fromLTRB(16, 14, 10, 14),
      decoration: BoxDecoration(
        color: Colors.white,
        border: Border.all(color: const Color(0xFFDCE3EC)),
        borderRadius: const BorderRadius.all(Radius.circular(8)),
      ),
      child: Row(children: [
        Icon(icon, color: const Color(0xFF52637A), size: 20),
        const SizedBox(width: 12),
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(label,
                  style: const TextStyle(color: Color(0xFF667085), fontSize: 12)),
              const SizedBox(height: 4),
              Text(value.isEmpty ? '--' : value,
                  style: const TextStyle(
                      color: Color(0xFF14233A),
                      fontSize: 20,
                      fontWeight: FontWeight.w600)),
            ],
          ),
        ),
        IconButton(
          tooltip: '复制',
          onPressed: value.isEmpty
              ? null
              : () {
                  Clipboard.setData(ClipboardData(text: value));
                  showToast(translate('Copied'));
                },
          icon: const Icon(Icons.copy_outlined, size: 19),
        ),
      ]),
    );
  }
}
