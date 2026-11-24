# Reverse engineering: Orca (stablyai/orca)

- Repo: https://github.com/stablyai/orca, commit `70a1f42a33125bc935d9a55796377e2015498b16` (2026-11-23)
- Lisensi: MIT (Lovecast Inc.). Aturan reuse tetap seperti `docs/references.md`: pelajari perilakunya,
  jangan salin kode, prompt, atau aset.
- Metode: membaca kode sumber dan dokumentasi bawaan; tidak ada yang dijalankan. Label: **Observed** =
  terlihat langsung di file yang disebut; **Inferred** = disimpulkan; **Unknown** = belum diverifikasi.
- Ukuran: 34.141 file, sebagian besar TypeScript (27.190 `.ts`, 4.202 `.tsx`).

## Jawaban singkat

Orca adalah **IDE desktop (Electron) yang mengorkestrasi agent CLI yang sudah ada** (Claude Code, Codex,
Gemini CLI, oh-my-pi, dan ±30 lainnya). Orca sendiri tidak memanggil API model untuk menulis kode. Ia
menjalankan program agent resmi di terminal (PTY) atau mode chat headless, di workspace/worktree masing-masing,
lalu mengatur siapa mengerjakan apa lewat database SQLite dan "kotak surat" antar agent. Yang memecah
pekerjaan adalah **agent LLM koordinator** yang memanggil perintah `orca orchestration ...`, bukan kode
penjadwal deterministik.

## Cara kerjanya, langkah demi langkah

| # | Langkah | Bukti |
| --- | --- | --- |
| 1 | Pengguna membuka agent (mis. Claude Code) di terminal Orca. Agent itu menjadi **koordinator**. | Observed: `skill-guides/orchestration.md` ("Recipe", "When Orca wakes you") |
| 2 | Koordinator memuat panduan lewat `orca skills get orchestration` (sekali), lalu membuat Run dan Task, opsional dengan dependensi (`task-create --deps`), dan memulai Task yang siap (`task-list --ready`). | Observed: `skill-guides/orchestration/references/coordinator-loop.md` |
| 3 | `orca orchestration worker-start --agent <agent> --worktree current\|new-child\|new-top-level` membuka agent lain sebagai **worker** di workspace-nya, maksimal ±4 sekaligus. | Observed: `skill-guides/orchestration.md` |
| 4 | Prompt worker diawali **dispatch preamble**: id Task, id Dispatch (pemagar antar retry), cara bertanya (`ask`), cara eskalasi, dan cara selesai (`worker_done`, laporan **tiga kalimat**, `--outcome failed` kalau gagal). | Observed: `src/main/runtime/orchestration/workers/launch/worker-dispatch-preamble.ts` (`buildDispatchPreamble`) |
| 5 | Koordinator **mengakhiri gilirannya** dan tidak polling. Orca "membangunkan" koordinator saat ada surat (selesai, pertanyaan, eskalasi). | Observed: `skill-guides/orchestration.md` ("Never: Poll or run watch scripts") |
| 6 | Status agent diketahui dari dua saluran: **hook** per agent yang mengirim POST ke server loopback, dan **OSC 7501** (agent menulis statusnya ke stream terminal). | Observed: `src/main/agent-hooks/README.md` |
| 7 | State orkestrasi di SQLite: Run, Task (`pending/ready/dispatched/completed/failed/blocked`), Dispatch (`pending/dispatched/completed/failed/circuit_broken`), Message (`status, dispatch, worker_done, merge_ready, escalation, handoff, decision_gate, question, heartbeat`), Question, Decision Gate, Mutation Receipt (idempotensi `request_id` + hash payload). | Observed: `src/main/runtime/orchestration/db/orchestration-records.ts`; `new Database(dbPath)` di `db/orchestration-db.ts:63` |
| 8 | Ada juga `Coordinator` di kode (fase `decomposing → dispatching → monitoring → merging → done`, poll 2 detik, maksimal 4 paralel). | Observed: `src/main/runtime/orchestration/coordinator/coordinator.ts`. Inferred: jalur lama/otomatis; jalur utama yang didokumentasikan adalah koordinator LLM |
| 9 | Agent dijalankan headless untuk tugas sekali jalan: Claude Code dengan `-p --output-format ...`, Codex dengan `exec --ephemeral --skip-git-repo-check` (prompt lewat stdin), Gemini dengan `--acp --approval-mode default`. | Observed: `src/shared/source-control/ai/generation/commit-message-agent-specs-primary.ts`, `src/main/gemini/gemini-acp-launch-spec.ts:44`; mode chat Codex lewat `app-server` (`src/main/codex-cli/codex-read-only-app-server-args.ts`) |
| 10 | Login langganan dilakukan dengan **menjalankan perintah login resmi CLI** (`claude auth login --claudeai`) di folder konfigurasi per akun. | Observed: `src/main/claude-accounts/sign-in/claude-login-session.ts:69` |

## Yang layak ditiru (diimplementasikan ulang, bukan disalin)

| Ide Orca | Kenapa berguna | Di Caecilia |
| --- | --- | --- |
| Worker = agent CLI resmi yang login sendiri | cara sah memakai langganan Claude/Codex/Gemini tanpa menyentuh kredensial | ADR-0018, prompt 36–38 |
| Dispatch preamble + laporan terstruktur tiga kalimat | hemat token koordinator; hasil bisa diproses mesin | prompt 39 |
| Id Dispatch sebagai pagar antar retry | laporan dari attempt lama tidak bisa menyelesaikan attempt baru | prompt 39 (memakai revisi lease yang sudah ada) |
| Tanpa polling; dibangunkan oleh event | koordinator tidak membakar token saat menunggu | sudah: orchestrator deterministik; prompt 40 (tanya-jawab) dan 42 (`caecilia wait`) |
| Ask/escalation dari worker | worker berhenti menebak saat ragu | prompt 40 |
| Status dari hook + protokol terminal | tahu agent sedang menunggu izin / selesai | prompt 37–38 (dari stream JSON), lanjutan di M7 |
| Panduan dimuat sekali, referensi dimuat sesuai kebutuhan | prompt worker tetap pendek | prompt 41 (budget prompt), 42 (`caecilia guide`) |
| Mutation receipt (request id + hash payload) | perintah yang diulang tidak dieksekusi dua kali | sudah: `idempotency_key` + outbox |

## Yang ditolak

| Pola Orca | Bukti | Alasan |
| --- | --- | --- |
| Membaca kredensial OAuth Claude Code dari keychain (`Claude Code-credentials`) | Observed: `src/main/claude-accounts/runtime-auth/keychain.ts` | ADR-0007: harness tidak membaca atau menyimpan kredensial langganan |
| Banyak akun langganan + pemilihan akun berdasarkan rate limit | Observed: `src/main/claude-accounts/selection/claude-account-selection.ts` (memakai `RateLimitService`, `readClaudeOAuthCredentials`) | Langganan ditujukan untuk pemakaian individu biasa; memutar akun untuk menghindari batas berisiko melanggar terms |
| Koordinator LLM yang memecah dan memantau semuanya | Observed: alur `skill-guides/orchestration.md` | Tidak ditolak mentah, tetapi **bukan default**: penjadwalan deterministik di kode lebih murah token dan bisa diuji (ADR-0019) |

## Perbandingan arsitektur

| Aspek | Orca | Caecilia Harness |
| --- | --- | --- |
| Siapa memecah pekerjaan | agent LLM koordinator, terus menerus | planner LLM **sekali**, lalu scheduler deterministik di kode |
| Siapa menulis kode | agent CLI eksternal | native worker (API key) **atau** agent CLI eksternal (M5) |
| Isolasi | worktree opsional (`current` / `new-child`) | worktree per lease, wajib untuk task yang menulis |
| Integrasi | worker/koordinator membuat PR / merge | integration gate serial: rebase → check → fast-forward CAS |
| State | SQLite, tabel mutable + receipt | SQLite event-sourced + proyeksi + outbox |
| Antarmuka | GUI Electron + CLI `orca` | CLI → TUI → GUI (ADR-0015) |

## Unknown

- Apakah koordinator kode (`Coordinator`) masih dipakai di alur produk utama; perlu menelusuri pemanggil RPC-nya.
- Format lengkap output stream JSON setiap agent CLI (berubah antar versi). Prompt 37–38 meminta verifikasi
  dengan `--help` dan rekaman output dari versi yang terpasang.
- Ketentuan Codex (login ChatGPT) dan Gemini CLI (login akun Google) untuk dijalankan oleh tool lain. Lihat
  keputusan D14.
