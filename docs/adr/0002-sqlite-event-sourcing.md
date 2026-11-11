# ADR-0002: SQLite WAL, satu penulis, event-sourced

- Status: Diterima
- Tanggal: 2026-11-10

## Konteks

Harness ini single-user dan lokal. Ia harus bisa merekonstruksi state setelah crash dan menampilkan riwayat di TUI.

## Keputusan

State berada di satu database SQLite mode WAL. Orchestrator adalah satu-satunya penulis. Setiap perubahan adalah append ke `events`; tabel seperti `tasks` adalah proyeksi yang diperbarui di transaksi yang sama. Pembaca (TUI, `caecilia status`) boleh membuka koneksi read-only.

## Alternatif yang ditolak

Postgres; Redis Streams; message broker eksternal.

## Konsekuensi

Multi-user dan multi-mesin di luar cakupan sampai ada ADR baru. Worker tidak pernah membuka DB.
