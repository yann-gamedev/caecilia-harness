# ADR-0015: `caecilia` sebagai perintah global, sesi interaktif, dan GUI yang pasti dibangun

- Status: Diterima
- Tanggal: 2026-11-23
- Mengubah: ADR-0012 (GUI tidak lagi opsional)

## Konteks

Pemilik ingin Caecilia Harness dipakai seperti Claude Code dan Gemini CLI: diketik langsung di terminal
(`cmd`, PowerShell, bash) dari folder proyek mana pun, membuka sesi interaktif kalau dipanggil tanpa argumen,
dan bisa dipakai non-interaktif dari skrip. Pemilik juga memutuskan GUI desktop **akan** dibuat, bukan
opsional. Saat ini CLI hanya bisa dipanggil lewat `uv run caecilia` dari dalam repo harness, dan setiap
perintah wajib diberi `--repo` dan `--check`.

## Keputusan

1. **Perintah global.** `caecilia` dipasang sebagai tool di PATH pengguna (`uv tool install`), bisa
   dipanggil dari folder mana pun. Folder kerja saat ini menjadi repo default.
2. **Konfigurasi proyek** di `.caecilia.toml` (root repo yang dikerjakan): `check`, `worker`, `provider`,
   `model`, `max_parallel`, `target`, slot, dan daftar perintah yang boleh disetujui otomatis di mode
   non-interaktif. Prioritas: opsi CLI → `.caecilia.toml` → default. Konfigurasi tidak pernah berisi API key.
3. **Tiga cara pakai, satu core:**

| Pemanggilan | Perilaku | Dibangun di |
| --- | --- | --- |
| `caecilia` | sesi interaktif: ketik goal, lihat plan, setujui, pantau run, slash command | CLI-02 (mode baris), M7-02 (layar penuh Textual) |
| `caecilia -p "goal"` | non-interaktif untuk skrip/CI: plan → run → cetak hasil, exit code | CLI-02 |
| `caecilia run/plan/status/graph/doc ...` | subcommand yang sudah ada | M1–M3 |

4. **Mode non-interaktif tetap aman:** tanpa TTY, approval ditolak kecuali perintahnya cocok dengan
   `--allow-cmd` atau `allow_commands` di `.caecilia.toml` (prefix argv, bukan pola shell). Merge tetap
   butuh `--auto-merge` eksplisit.
5. **GUI desktop pasti dibangun (M8)** sebagai frontend ketiga di atas control API lokal yang sama dengan
   TUI. Teknologi GUI dipilih pemilik (keputusan D13) berdasarkan perbandingan dan spike di prompt M8-01.
   Desain layar dibuat pemilik lebih dulu; spesifikasi control API (M7-01) wajib mencakup semua data dan aksi
   yang dibutuhkan desain itu.
6. Semua frontend (sesi baris, TUI, GUI, mode `-p`) hanya memanggil fungsi aplikasi di luar `interfaces/`
   (ADR-0012 tetap berlaku untuk aturan ini).

## Alternatif yang ditolak

- Membiarkan pengguna memanggil `uv run` dari repo harness: tidak bisa dipakai di proyek lain dengan nyaman.
- Langsung TUI layar penuh tanpa mode baris: lebih lama sebelum sesi interaktif bisa dipakai, dan mode baris
  tetap dibutuhkan untuk terminal sederhana dan pembaca layar.
- Mode `-p` yang otomatis menyetujui semua perintah: melanggar ADR-0008.

## Konsekuensi

- Perintah harus berjalan benar dari folder mana pun (path relatif terhadap cwd, bukan repo harness).
- Ada file konfigurasi baru yang dibaca dari repo yang dikerjakan; isinya diperlakukan sebagai konfigurasi
  pengguna, tetapi tetap divalidasi ketat (field tak dikenal ditolak).
- Control API M7 menjadi kontrak publik untuk dua frontend; perubahan wajib berversi.
