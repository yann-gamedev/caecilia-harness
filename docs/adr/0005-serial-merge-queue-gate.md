# ADR-0005: Merge queue serial sebagai integration gate

- Status: Diterima
- Tanggal: 2026-11-10

## Konteks

Dua diff bisa lolos tes masing-masing tetapi rusak saat digabung. Tidak ada repo referensi yang punya gate berbasis kontrak + tes.

## Keputusan

Integrasi berupa antrean serial. Untuk setiap kandidat: rebase ke `main` terbaru di integration worktree, jalankan lint/tipe/tes, jalankan contract check, lalu fast-forward `main`. Gagal → REWORK dengan laporan, atau NEEDS_HUMAN kalau konflik tak bisa diselesaikan. Rescue branch menyimpan commit worker saat gagal. Di MVP fast-forward terakhir meminta konfirmasi pengguna kecuali `--auto-merge` diset.

## Alternatif yang ditolak

Merge paralel; merge tanpa tes.

## Konsekuensi

Throughput integrasi satu per satu; perintah tes di gate harus tetap cepat.
