# Keputusan terbuka

Keputusan milik Chris. Selama belum diputuskan, agent memakai **Default sementara**.
Setelah diputuskan: isi kolom "Keputusan", lalu bila perlu ubah ADR terkait.

| ID | Pertanyaan | Default sementara | Keputusan |
| --- | --- | --- | --- |
| D1 | Adapter agent eksternal (Claude Code, Codex) masuk MVP? | Tidak. Native worker dulu; eksternal di M5. | |
| D2 | Provider model pertama untuk native worker | Belum dipilih. Prompt 14 (M1-14) terblokir; M1-01 s.d. M1-13 jalan dengan provider dan worker palsu. | |
| D3 | Bahasa implementasi | Python 3.12 (ADR-0001). | |
| D4 | Otonomi merge | Fast-forward ke branch utama minta konfirmasi; `--auto-merge` opsional. | |
| D5 | Default approval | `ask` untuk exec (ADR-0008). | |
| D6 | OS target MVP | Windows + Linux. Sandbox hanya Linux (bwrap); di Windows exec selalu `ask` kecuali `--unsafe-no-sandbox`. | |
| D7 | Lisensi dan distribusi | Belum dipilih; belum ada file LICENSE. Jangan publikasikan repo sebelum diputuskan. | |
| D8 | Nama produk | "Caecilia Harness". Hindari nama/logo DeepSeek, Claude Code, Anthropic, OpenAI. | |
| D9 | Multi-user di roadmap | Tidak (SQLite lokal, ADR-0002). | |
| D10 | Bentuk antarmuka | — | **Diputuskan 2026-11-11:** TUI dulu (M7). GUI desktop berjendela menyusul nanti sebagai frontend kedua (M8), dimulai hanya kalau pemilik memutuskan. Lihat ADR-0012. |
