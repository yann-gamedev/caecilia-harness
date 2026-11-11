# ADR-0010: Slot konkurensi hanya dipegang selama operasi

- Status: Diterima
- Tanggal: 2026-11-10

## Konteks

oh-my-pi mengalami deadlock ketika parent memegang slot provider sepanjang umurnya sambil menunggu anak yang juga butuh slot (issue #3749).

## Keputusan

Scheduler punya semaphore terpisah untuk request LLM (global dan per provider), exec, dan merge. Slot diambil tepat sebelum operasi dan dilepas tepat setelahnya. Tidak ada yang memegang slot sambil menunggu task lain.

## Alternatif yang ditolak

Satu slot per umur agent.

## Konsekuensi

Scheduler mengelola beberapa semaphore dan wajib dites dengan spawn tree yang lebih lebar dari batasnya.
