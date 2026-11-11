# ADR-0011: Event durable vs ephemeral

- Status: Diterima
- Tanggal: 2026-11-10

## Konteks

Menulis setiap token yang di-stream ke SQLite membengkakkan DB dan beban tulis.

## Keputusan

Hanya fakta durable yang masuk `events`: perubahan state, ringkasan, checkpoint, approval, usage, laporan gate. Stream token dan tick progres bersifat ephemeral: disimpan di ring buffer memori dan file log per worker di CAECILIA_HOME.

## Alternatif yang ditolak

Menyimpan setiap token.

## Konsekuensi

Replay transkrip penuh bergantung pada file log worker, bukan DB.
