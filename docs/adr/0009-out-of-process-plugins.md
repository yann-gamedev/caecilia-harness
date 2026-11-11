# ADR-0009: Plugin pihak ketiga berjalan di luar proses

- Status: Diterima
- Tanggal: 2026-11-10

## Konteks

Kedua repo referensi memuat ekstensi in-process, sehingga plugin mewarisi semua hak host.

## Keputusan

Plugin pihak ketiga berjalan sebagai proses terpisah yang berbicara JSON-RPC lewat stdio dengan manifest izin. Skill adalah data berversi dengan hash konten. Server MCP dipakai lewat SDK resmi; tool diberi nama `mcp__<server>__<tool>` dari config lokal (bukan nama dari server remote) dan setiap tool baru butuh approval.

## Alternatif yang ditolak

Plugin in-process.

## Konsekuensi

Overhead IPC dan API plugin yang lebih sempit.
