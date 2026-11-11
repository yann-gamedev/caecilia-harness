# ADR-0008: Default aman: workspace-write + ask

- Status: Diterima
- Tanggal: 2026-11-10

## Konteks

Agent mengeksekusi output model. oh-my-pi secara default auto-approve semuanya; deepseek-harness default workspace-write dengan approval `ask` dan gagal tertutup kalau sandbox tidak tersedia.

## Keputusan

Kebijakan default: penulisan file hanya di worktree task; eksekusi perintah butuh approval (`ask`) yang bisa diingat per pola perintah. Linux memakai bubblewrap. Di platform tanpa sandbox (Windows di MVP), runner menolak auto-approve exec kecuali pengguna memberi `--unsafe-no-sandbox`. Jaringan untuk exec mati secara default selama sandbox mendukungnya.

## Alternatif yang ditolak

Auto-approve semuanya secara default.

## Konsekuensi

Lebih banyak prompt di awal; dikurangi dengan approval yang diingat dan antrean approval.
