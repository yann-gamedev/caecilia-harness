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
- [x] M2-05
- [x] M2-06
- [x] M2-07
- [ ] M2-08

Diterima jika (milestone): dua task independen berjalan bertumpuk waktunya dengan log terpisah; graf
bersiklus ditolak; spawn tree yang lebih lebar dari batas slot + 1 tidak deadlock; crash saat dua task
berjalan tidak menghasilkan merge ganda; benchmark menghasilkan angka untuk keputusan D11.

Ditunda dari M2 (dicatat agar tidak hilang): worker mengusulkan task atau edge baru saat berjalan
(ADR-0003) → M3 bersama konteks bersama; backoff waktu untuk RETRY_WAIT → M4; menaikkan budget run yang
sudah habis lewat revisi graf → M3; `max_cost_usd` (butuh tabel harga) → setelah M2-B.

## Provider tambahan (opsional)

| ID | Task | Prompt | Dependensi |
| --- | --- | --- | --- |
| PRV-01 | Provider Gemini + pilihan provider per run | [23](prompts/23-provider-gemini.md) | M2-04, keputusan D12 |

- [ ] PRV-01

## M3 — Konteks bersama

Keputusan desain: ADR-0014 dan bagian "Konteks bersama" di `docs/architecture.md`.

| ID | Task | Prompt | Dependensi |
| --- | --- | --- | --- |
| M3-01 | Dokumen berversi: store, CAS, CLI `doc` | [24](prompts/24-dokumen-berversi.md) | M2-07 |
| M3-02 | Pin konteks + context pack builder | [25](prompts/25-pin-dan-context-pack.md) | M3-01 |
| M3-03 | Invalidasi: `task.context_stale` dan jalur STALE | [26](prompts/26-invalidasi-stale.md) | M3-02 |
| M3-04 | Usulan worker: dokumen, task, dependensi | [27](prompts/27-usulan-worker.md) | M3-03 |
| M3-05 | Handoff blocker + menaikkan budget run | [28](prompts/28-handoff-dan-budget.md) | M3-02 |
| M3-06 | Chaos dan tes konkurensi konteks bersama | [29](prompts/29-chaos-konteks.md) | M3-04, M3-05 |

Checklist:

- [ ] M3-01
- [ ] M3-02
- [ ] M3-03
- [ ] M3-04
- [ ] M3-05
- [ ] M3-06

Diterima jika (milestone): dokumen yang di-pin berubah saat task berjalan → task STALE dalam satu siklus
event; CAS basi ditolak; context pack tidak pernah berisi transkrip worker lain; usulan worker tidak pernah
diterapkan tanpa approval; crash di tengah invalidasi tidak meninggalkan task dengan pin yang salah.

## CLI global dan sesi interaktif (ADR-0015)

Boleh dikerjakan sebelum M3 selesai; tidak bergantung pada konteks bersama.

| ID | Task | Prompt | Dependensi |
| --- | --- | --- | --- |
| CLI-01 | Perintah global `caecilia` + `.caecilia.toml` + `caecilia init` | [30](prompts/30-perintah-global.md) | M2-07 |
| CLI-02 | Sesi interaktif mode baris (`caecilia`) + mode skrip (`caecilia -p`) | [31](prompts/31-sesi-interaktif.md) | CLI-01 |

- [ ] CLI-01
- [ ] CLI-02

## M7 — Control API lokal dan Retro Dark TUI

| ID | Task | Prompt | Dependensi |
| --- | --- | --- | --- |
| M7-01 | Control API lokal berversi (approve, cancel, subscribe, query) | [32](prompts/32-control-api.md) | CLI-02 |
| M7-02 | Retro Dark TUI layar penuh di atas control API | [33](prompts/33-tui-textual.md) | M7-01 |

- [ ] M7-01
- [ ] M7-02

Catatan urutan: M7 tidak menunggu M4–M6. Fitur M4–M6 yang menambah data atau aksi baru wajib menambah
endpoint control API dengan versi baru.

## M8 — GUI desktop (pasti dibangun, ADR-0015)

| ID | Task | Prompt | Dependensi |
| --- | --- | --- | --- |
| M8-01 | Perbandingan teknologi + spike GUI → keputusan D13 | [34](prompts/34-gui-teknologi-dan-spike.md) | M7-01, desain layar dari pemilik |
| M8-02 | Kerangka GUI: daftar run, task graph, antrean approval | [35](prompts/35-gui-kerangka.md) | M8-01, keputusan D13 |

- [ ] M8-01
- [ ] M8-02

Layar GUI berikutnya (editor plan, dokumen, usage/budget, pengaturan) dipecah jadi prompt setelah M8-02
dan desain pemilik tersedia.

## M4–M6 (kasar; dipecah jadi prompt setelah milestone sebelumnya selesai dan disetujui pemilik)

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
## Bug terbuka

Bug yang sudah terlihat tetapi belum diketahui penyebabnya. Kalau muncul lagi, simpan output lengkapnya
(`uv run pytest tests/chaos/test_parallel_crash.py -p no:randomly > hasil.txt 2>&1`) dan lampirkan.

- [ ] **BUG-01 — Kegagalan sesekali di suite chaos paralel** (`tests/chaos/test_parallel_crash.py`).
  - Terlihat: 2026-11-23, saat M2-07, **2 kali dari sekitar 7 putaran pertama** setelah perbaikan
    `fix(core): worktree baru per lease ...` (`df0eb2a`). Keduanya menampilkan `1 failed, 4 passed`.
  - Tidak diketahui: skenario mana yang gagal dan pesan assert-nya; output kedua putaran itu tidak tersimpan.
  - Setelahnya: **37 putaran berturut-turut lulus** (PowerShell dan Git Bash, juga saat mesin dibebani tes
    lain), sehingga kegagalannya belum bisa direproduksi.
  - Dugaan (belum terbukti): asumsi timing di skenario, misalnya `states["a"] in ("LEASED", "RUNNING")` saat
    hold `RUNNING@b`, atau `states["b"] == "RUNNING"` saat hold `MERGED@a` dengan delay `b=4`; bisa juga
    sisa efek recovery yang belum tertutup perbaikan di atas.
  - Dampak: belum ada bukti merge ganda atau state rusak; invarian (`tests/chaos/invariants.py`) lulus di
    semua putaran yang outputnya tersimpan.
  - Selesai jika: penyebabnya diketahui dan diperbaiki dengan tes yang mereproduksinya, atau terbukti hanya
    asumsi timing di tes lalu skenarionya dibuat deterministik.

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
- 2026-11-23 — M2-05 selesai: `Planner` (tool `submit_plan` tanpa `$ref`, validasi graf, sampai 3 ronde perbaikan), `summarize_repo` berlabel data tak tepercaya, `security/untrusted.py`, `bootstrap.plan_goal`, dan `caecilia plan GOAL` di grup yang sama dengan `plan validate`.
- 2026-11-23 — M2-06 selesai: operasi revisi graf (`add_task`, `set_blockers`, `cancel_task`, `retry_task`), `Orchestrator.revise_graph` atomik dengan CAS dan validasi seluruh graf, proyeksi `task.blockers_changed`/`run.reopened`, perintah `caecilia graph show/add/deps/cancel/retry`.
- 2026-11-23 — M2-07 selesai: hook chaos `TITIK@id` dan `BUDGET_EXCEEDED`, `check_invariants`, 5 skenario crash paralel, uji stres slot (5 seed + pertanyaan tertunda). Bug recovery ditemukan dan diperbaiki: spawn yang terputus setelah worktree dibuat membuat lease berikutnya gagal; nama worktree kini per lease.
- 2026-11-23 — BUG-01 dicatat: kegagalan sesekali di suite chaos paralel (2 dari ~7 putaran awal, lalu 37 putaran lulus); penyebab belum diketahui.
- 2026-11-23 — M3 dipecah menjadi 6 task (prompt 24–29) dengan ADR-0014; prompt 23 (provider Gemini, opsional) dan keputusan D12 ditambahkan.
- 2026-11-23 — ADR-0015: `caecilia` jadi perintah global dengan sesi interaktif dan mode `-p`; GUI desktop pasti dibangun. Prompt 30–35 ditambahkan (CLI-01/02, M7-01/02, M8-01/02); keputusan D13 dibuka.
