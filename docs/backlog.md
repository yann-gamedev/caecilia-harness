# Backlog

Satu task = satu prompt di `docs/prompts/` = satu branch = satu sesi agent. Detail lengkap (tujuan, yang
dikerjakan, hasil, pengecekan, commit & branch) ada di file prompt; backlog ini hanya urutan dan dependensi.

Agent: kerjakan task pertama yang belum dicentang dan semua dependensinya sudah dicentang.
Task dengan dependensi yang sama boleh dikerjakan dalam urutan apa pun (misalnya 04 dan 05).

## M0 — Scaffold

- [x] **M0-01 — Scaffold repo, kontrak, guardrail** (dibuat bersama audit v0.2)
- [x] **M0-02 — Persiapan repo di laptop** → [prompt 00](prompts/00-persiapan-repo.md)
  - Dependensi: M0-01

## M1 — MVP core: satu task end-to-end, tahan crash

| ID | Task | Prompt | Dependensi |
| --- | --- | --- | --- |
| M1-01 | Store: skema dan migrasi | [01](prompts/01-store-schema-migrasi.md) | M0-02 |
| M1-02 | Store: append event, CAS, idempotensi | [02](prompts/02-store-append-event.md) | M1-01 |
| M1-03 | Proyeksi run dan task + penegakan tabel state | [03](prompts/03-proyeksi-task.md) | M1-02 |
| M1-04 | Provider palsu + contract suite provider | [04](prompts/04-fake-provider.md) | M0-02 |
| M1-05 | Workspace: git worktree, artefak, capture diff | [05](prompts/05-git-worktree.md) | M0-02 |
| M1-06 | Exec runner + kebijakan approval + scrub env | [06](prompts/06-exec-runner-approval.md) | M1-05 |
| M1-07 | Tool file untuk native worker | [07](prompts/07-tool-native-worker.md) | M1-06 |
| M1-08 | Loop native worker | [08](prompts/08-loop-native-worker.md) | M1-04, M1-07 |
| M1-09 | Gate sederhana + fast-forward CAS | [09](prompts/09-gate-sederhana.md) | M1-05 |
| M1-10 | Orchestrator run loop + outbox + worker palsu | [10](prompts/10-orchestrator-run-loop.md) | M1-03, M1-08, M1-09 |
| M1-11 | Lease, heartbeat, dan recovery | [11](prompts/11-lease-recovery.md) | M1-10 |
| M1-12 | CLI: run, status, approval inline | [12](prompts/12-cli-run-status.md) | M1-11 |
| M1-13 | Suite chaos: crash di setiap state | [13](prompts/13-chaos-test.md) | M1-12 |
| M1-14 | Provider model sungguhan pertama | [14](prompts/14-provider-pertama.md) | M1-04, keputusan D2 |

Checklist:

- [x] M1-01
- [ ] M1-02
- [ ] M1-03
- [ ] M1-04
- [ ] M1-05
- [ ] M1-06
- [ ] M1-07
- [ ] M1-08
- [ ] M1-09
- [ ] M1-10
- [ ] M1-11
- [ ] M1-12
- [ ] M1-13
- [ ] M1-14

## M2–M7 (kasar; dipecah jadi prompt setelah M1 selesai dan disetujui pemilik)

- [ ] **M2 — Orkestrasi paralel.** Task graph berrevisi + validasi (hilang, duplikat, diri sendiri,
  siklus); scheduler dengan slot global, per provider, exec, dan merge; cek write set beririsan;
  dua worktree bersamaan.
  - Diterima jika: dua task independen berjalan bertumpuk waktunya dengan log terpisah; graf bersiklus
    ditolak; spawn tree yang lebih lebar dari batas slot + 1 tidak deadlock.
- [ ] **M2-B — Benchmark serial vs paralel.** Workload W1 (frontend/backend independen), W2 (refactor
  saling bergantung), W3 (banyak edit kecil): wall time, token, biaya, konflik, kegagalan gate;
  ≥ 5 run per mode; median + rentang.
- [ ] **M3 — Konteks bersama.** `documents` dengan CAS, `context_pins`, context pack builder,
  `task.context_stale`, ringkasan handoff.
  - Diterima jika: dokumen yang di-pin berubah saat task berjalan → task STALE dalam satu siklus event;
    CAS basi ditolak; context pack tidak pernah berisi transkrip worker lain.
- [ ] **M4 — Integration gate penuh.** Merge queue serial, rebase otomatis, lint/tipe/tes,
  contract diff, rescue branch.
  - Diterima jika: dua diff yang lolos sendiri-sendiri tetapi bentrok → yang kedua gagal dengan laporan;
    crash di tengah merge tidak pernah merusak branch target.
- [ ] **M5 — Adapter agent eksternal.** Contract suite worker; adapter Claude Agent SDK (API key),
  Codex app-server, ACP generik.
  - Diterima jika: semua lulus contract suite yang sama; cancel membunuh process group dalam batas grace;
    harness menghitung diff walau adapter tidak melapor.
- [ ] **M6 — Plugin, skill, MCP.** Plugin host out-of-process dengan manifest izin; skill berversi;
  MCP client dengan approval per tool.
  - Diterima jika: plugin yang meminta izin di luar manifest ditolak; tool MCP baru butuh approval;
    nama tool remote tidak bisa membayangi tool lokal.
- [ ] **M7 — Retro Dark TUI.** Aplikasi Textual: graph / sesi / inspector / antrean approval / ticker
  event; breakpoint ≥160, 120–159, 80–119, <80 kolom; `--plain`; tema high-contrast; control API lokal
  yang netral terhadap frontend untuk approve/cancel/subscribe dari proses lain (ADR-0012).
  - Diterima jika: snapshot test per breakpoint; semua aksi bisa lewat keyboard dan CLI; render tidak
    pernah memblokir loop > 50 ms dengan 5 sesi streaming; semua aksi TUI juga bisa dilakukan lewat
    control API tanpa TUI (dites dengan klien API sederhana).
- [ ] **M8 — GUI desktop (opsional, menunggu keputusan pemilik).** Frontend kedua berjendela di atas
  control API dari M7. Teknologi dipilih lewat ADR baru saat dimulai. Tidak ada perubahan di core.
  - Diterima jika: semua aksi utama TUI (lihat graph, pantau sesi, approve, cancel) tersedia di GUI; TUI
    tetap berfungsi; tidak ada import dari `interfaces/tui` ke GUI atau sebaliknya.

## Log

- 2026-11-10 — M0-01 scaffold dibuat dari audit v0.2.
- 2026-11-11 — Dokumen diterjemahkan ke Bahasa Indonesia; M1 dipecah menjadi 14 task dengan prompt.
- 2026-11-11 — Keputusan D10 / ADR-0012: TUI dulu, GUI desktop menyusul sebagai M8 (opsional).
- 2026-11-11 — M0-02 selesai: CI Windows/Linux dipasang dan pengecekan lokal lulus.
- 2026-11-11 — M1-01 selesai: `store/db.py` membuka SQLite WAL, migrasi skema v1 bertransaksi, koneksi read-only.
