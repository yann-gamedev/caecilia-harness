# ADR-0012: TUI dulu, GUI desktop menyusul sebagai frontend kedua

- Status: Diterima
- Tanggal: 2026-11-11

## Konteks

PDF v0.1 menetapkan Retro Dark Terminal UI. Pemilik juga ingin membuka jalan ke aplikasi desktop
berjendela, tetapi baru nanti, dan dengan dua frontend terpisah: TUI dan GUI. Kalau logika aplikasi
terlanjur menempel di CLI/TUI, GUI nanti terpaksa menyalin logika itu atau merombak core.

## Keputusan

1. **Sekarang:** antarmuka resmi adalah CLI (M1) dan Retro Dark TUI dengan Textual (M7).
2. **Nanti (M8, opsional):** GUI desktop berjendela sebagai frontend kedua. Dimulai hanya ketika pemilik
   memutuskan; teknologi GUI (mis. Tauri, Electron, PySide6) dipilih lewat ADR baru saat itu.
3. **Kedua frontend adalah klien tipis dari core yang sama.** Logika aplikasi (membuat run, memilih base
   commit, approval, cancel, membaca status) berada di `core`, `bootstrap.py`, dan modul aplikasi di luar
   `interfaces/`. Paket `interfaces/` hanya berisi parsing input, format tampilan, dan implementasi
   `UserPrompter`.
4. **Control API lokal di M7.** Perintah lintas proses (approve, cancel, subscribe event) dirancang sebagai
   API lokal yang netral terhadap frontend: pesan JSON berversi lewat named pipe (Windows) / Unix socket,
   atau HTTP di `127.0.0.1` dengan token per sesi. TUI menjadi klien pertama API ini; GUI nanti memakai
   API yang sama tanpa perubahan di core. Detail protokol ditetapkan lewat ADR tersendiri saat M7 dipecah
   jadi prompt.
5. Data untuk tampilan dibaca dari proyeksi DB (read-only) atau dari stream event API, tidak pernah dari
   state internal milik salah satu frontend.

## Alternatif yang ditolak

- Langsung GUI tanpa TUI: menambah stack dan waktu sebelum core terbukti andal.
- TUI saja tanpa jalur GUI: memaksa refactor besar kalau pemilik nanti ingin aplikasi berjendela.
- GUI yang memanggil fungsi TUI secara langsung: membuat kedua frontend saling bergantung.

## Konsekuensi

- Fitur yang terlihat di TUI wajib punya fungsi aplikasi yang bisa dipanggil tanpa TUI (dites tanpa terminal).
- `interfaces/` tetap tidak diimpor package mana pun (sudah ditegakkan import-linter).
- Control API menambah kerja di M7, tetapi membuat M8 terbatas pada lapisan tampilan.
