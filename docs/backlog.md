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
- [x] M1-02
- [x] M1-03
- [x] M1-04
- [x] M1-05
- [x] M1-06
- [x] M1-07
- [x] M1-08
- [x] M1-09
- [x] M1-10
- [x] M1-11
- [x] M1-12
- [x] M1-13
- [x] M1-14

## M2 — Orkestrasi paralel

Keputusan desain: ADR-0013 dan bagian "Rencana run dan penjadwalan paralel" di `docs/architecture.md`.

| ID | Task | Prompt | Dependensi |
| --- | --- | --- | --- |
| M2-01 | Glob, write set, dan validasi graf (core murni) | [15](prompts/15-graf-dan-write-set.md) | M1-14 |
| M2-02 | File plan TOML + run multi-task dengan dependensi (serial) | [16](prompts/16-plan-file-multi-task.md) | M2-01 |
| M2-03 | Scheduler paralel, slot merge, prompter serial | [17](prompts/17-scheduler-paralel.md) | M2-02 |
| M2-04 | Slot provider/exec, budget run, penegakan write set | [18](prompts/18-slot-budget-write-set.md) | M2-03 |
| M2-05 | Planner LLM: `caecilia plan` | [19](prompts/19-planner-llm.md) | M2-02 |
| M2-06 | Revisi graf saat run berhenti (CAS) | [20](prompts/20-revisi-graf.md) | M2-03 |
| M2-07 | Chaos dan tes konkurensi paralel | [21](prompts/21-chaos-paralel.md) | M2-04, M2-06 |
| M2-08 | Benchmark serial vs paralel (M2-B) | [22](prompts/22-benchmark.md) | M2-07, M2-05 |

Checklist:

- [x] M2-01
- [x] M2-02
- [x] M2-03
- [x] M2-04
- [ ] M2-05
- [ ] M2-06
- [ ] M2-07
- [ ] M2-08

Diterima jika (milestone): dua task independen berjalan bertumpuk waktunya dengan log terpisah; graf
bersiklus ditolak; spawn tree yang lebih lebar dari batas slot + 1 tidak deadlock; crash saat dua task
berjalan tidak menghasilkan merge ganda; benchmark menghasilkan angka untuk keputusan D11.

Ditunda dari M2 (dicatat agar tidak hilang): worker mengusulkan task atau edge baru saat berjalan
(ADR-0003) → M3 bersama konteks bersama; backoff waktu untuk RETRY_WAIT → M4; menaikkan budget run yang
sudah habis lewat revisi graf → M3; `max_cost_usd` (butuh tabel harga) → setelah M2-B.

## M3–M8 (kasar; dipecah jadi prompt setelah milestone sebelumnya selesai dan disetujui pemilik)

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
- 2026-11-11 — M1-02 selesai: `SqliteEventStore` dengan append atomik, CAS per stream, idempotency key, dan hook projector.
- 2026-11-11 — M1-03 selesai: proyeksi `runs`/`tasks`/`task_deps` dari event, validasi transisi dan revisi, `rebuild_projections`.
- 2026-11-11 — M1-04 selesai: `CancelFlag`, `FakeProvider` berbasis script, dan contract suite `ModelProviderContract`.
- 2026-11-11 — M1-05 selesai: artefak content-addressed, `run_git` async, `GitWorktreeBackend` (worktree per task, capture diff termasuk untracked), `DiffArtifact.head_commit`.
- 2026-11-11 — M1-06 selesai: `scrub_env`, `ApprovalPolicy`, deteksi sandbox bubblewrap, `ExecRunner` (batas worktree, approval, timeout + kill pohon proses, potong output).
- 2026-11-11 — M1-07 selesai: tool `read_file`/`write_file`/`edit_file`/`list_dir`/`run` untuk native worker, terkurung di worktree dan tanpa akses `.git`.
- 2026-11-11 — M1-08 selesai: `NativeWorker` (loop model/tool, budget, retry, cancel soft/hard, event buffer), `ToolCall` di `Message`; exec yang dibatalkan kini ikut dimatikan.
- 2026-11-11 — M1-09 selesai: `SimpleGate` (integration worktree, rebase + rescue branch, check command ber-env bersih, fast-forward CAS lewat update-ref atau ff-only).
- 2026-11-11 — M1-10 selesai: `Orchestrator` (run loop satu task, CAS per stream, outbox `spawn_worker`, gate dengan `gate.merging`, rework/retry), `FakeWorker`, `bootstrap.build_orchestrator`; port `Gate.check` kini menerima `before_merge`.
- 2026-11-11 — M1-11 selesai: `WriterLock`, lease task (acquire/renew/release/expire), heartbeat, `recover()`/`resume()` per state; worktree attempt ke-2 dst. bernama `<task>-<attempt>`; port `Gate.branch_head`.
- 2026-11-11 — M1-12 selesai: CLI `run`/`resume`/`status`/`runs`, `TerminalPrompter`, fungsi aplikasi di `bootstrap.py`; opsi run disimpan di `run.created` untuk `resume`.
- 2026-11-16 — M1-13 selesai: hook `CAECILIA_TEST_HOLD_AT` dan suite chaos (kill proses di 7 titik lalu `resume`); tidak ada bug recovery yang ditemukan.
- 2026-11-16 — M1-14 selesai: keputusan D2 = DeepSeek; `DeepSeekProvider` (httpx, API key), `security/keys.py` (keyring/env + redaksi), `caecilia key set/status`, `--worker native --model`; dependensi baru `httpx` dan `keyring`.
- 2026-11-16 — M2 dipecah menjadi 8 task (prompt 15–22); ADR-0013 dan keputusan D11 ditambahkan.
- 2026-11-16 — M2-01 selesai: `core/globs.py` (pencocokan glob, normalisasi, `may_overlap` konservatif) dan `core/graph.py` (validasi graf, urutan topologis, `ready`, `blocked_by_failure`, `conflicts`).
- 2026-11-16 — M2-02 selesai: `planning/plan.py` (model plan, baca/tulis TOML, `plan_to_specs`), run multi-task serial sadar dependensi, `graph.revised` revisi 1, base commit saat spawn, `caecilia run --plan` dan `caecilia plan validate`.
- 2026-11-18 — M2-03 selesai: scheduler paralel dengan batas worker dan konflik write set, gate serial, outbox per task tanpa spawn ganda, `SerializedPrompter`, input terminal nonblocking, `--max-parallel` tersimpan untuk resume, serta label task di log. Pemilik menyetujui daemon thread untuk pembatalan prompt dan bukti overlap/konkurensi sebagai pengganti batas waktu 1,8 detik.
- 2026-11-19 — M2-04 selesai: `LimitedProvider` dan slot exec bersama (diambil setelah approval), opsi `--provider-slots`/`--exec-slots` tersimpan untuk resume, budget token run dari event durable dengan penghentian lease baru, serta penolakan diff di luar write set sebelum gate dengan laporan rework. Pembatalan exec menunggu proses dan pembaca output selesai sebelum melepas slot.
