# ADR-0018: Worker dari agent CLI resmi, termasuk dengan langganan pengguna

- Status: Diterima
- Tanggal: 2026-11-24
- Catatan nomor: ADR-0016 dan ADR-0017 dipesan untuk prompt 32 (transport control API) dan prompt 34
  (teknologi GUI).

## Konteks

Pemilik ingin pekerjaan bisa dikerjakan dengan model lewat API key **atau** langganan (Claude, ChatGPT/Codex,
Gemini). Langganan tidak boleh dipakai dengan memanggil API langsung memakai token login (ADR-0007). Orca
(`docs/research/orca.md`) menunjukkan jalur yang lazim: harness menjalankan **program agent resmi** yang
login sendiri, lalu mengatur pekerjaannya. Halaman legal Claude Code menyatakan bahwa pengguna akhir boleh
login ke binary Claude Code resmi yang tidak dimodifikasi dengan langganannya sendiri.

## Keputusan

1. Ada dua jenis worker:
   - **native**: tool loop milik Caecilia, model lewat API key (DeepSeek sekarang, Gemini di prompt 23).
   - **agent CLI eksternal**: Claude Code (`claude`), Codex (`codex`), Gemini CLI (`gemini`), dan agent lain
     yang punya mode headless. Program dijalankan apa adanya di worktree task.
2. Agent CLI eksternal memakai login miliknya sendiri (langganan atau API key yang diatur di agent itu).
   Harness **tidak pernah**: membaca, menyalin, atau menyimpan file kredensial / item keychain agent;
   menjalankan alur login atas nama pengguna; memutar beberapa akun untuk menghindari rate limit; memodifikasi
   binary agent; atau meniru identitas klien.
3. Harness hanya boleh menanyakan status login lewat perintah resmi agent itu sendiri (kalau ada) dan
   menampilkan "sudah login / belum"; kalau belum, harness menyuruh pengguna menjalankan perintah login resmi.
4. Worker eksternal mengikuti kontrak yang sama dengan worker native: worktree sendiri, diff dihitung harness
   dari worktree, gate serial, laporan terstruktur (prompt 38). Izin agent dijalankan dalam mode
   non-interaktif yang **tidak** melewati semua izin (tidak memakai opsi "skip permissions") kecuali pemilik
   secara eksplisit memintanya per run.
5. Setiap profil agent mencatat versi CLI yang diverifikasi dan halaman terms vendor; pemakaian lewat
   langganan untuk agent tertentu baru diaktifkan setelah pemilik mengonfirmasinya di keputusan D14.

## Alternatif yang ditolak

- Memanggil API vendor dengan token login langganan: dilarang terms (ADR-0007).
- Membungkus TUI interaktif agent dengan screen scraping sebagai jalur utama: rapuh; mode headless/stream
  JSON dipakai lebih dulu, PTY hanya cadangan.

## Konsekuensi

- Pemakaian langganan tunduk pada batas pemakaian vendor; harness tidak berusaha menghindarinya.
- Format output agent berubah antar versi; adapter wajib punya tes rekaman per versi dan gagal dengan jelas
  untuk versi yang belum dikenal.
- Di Windows belum ada sandbox (ADR-0008): agent eksternal berjalan dengan izinnya sendiri di worktree.
