# ADR-0013: Rencana run dan penjadwalan paralel

- Status: Diterima
- Tanggal: 2026-11-16

## Konteks

M1 menjalankan satu task per run, semua task memakai `base_commit` milik run, dan `Orchestrator.run()`
mengerjakan task satu per satu. M2 harus menjalankan banyak task dengan dependensi, sebagian secara
paralel, tanpa merusak jaminan M1 (CAS per stream, outbox idempoten, recovery, gate serial).

## Keputusan

1. **Rencana run = file plan TOML** (`plan.toml`, format di `docs/architecture.md`). Pengguna bisa
   menulisnya sendiri; planner LLM (`caecilia plan`) hanya menghasilkan file dengan format yang sama dan
   tidak pernah langsung menjalankan run. TOML dipilih karena `tomllib` ada di standard library; penulisan
   file memakai serializer kecil milik sendiri, tanpa dependensi baru.
2. **Graf punya stream sendiri** `graph:<run_id>`. Event `graph.revised {revision, tasks, edges}` dicatat
   saat run dibuat (revisi 1) dan setiap kali graf diubah. Setiap revisi divalidasi penuh (id duplikat,
   blocker tidak ada, merujuk diri sendiri, siklus, pola write set tidak valid) sebelum di-append dengan
   CAS pada stream graf.
3. **Base commit dihitung saat spawn**, bukan saat run dibuat: head branch target ketika task di-lease.
   Task yang bergantung pada task lain dengan begitu melihat hasil merge blocker-nya.
4. **Write set** adalah daftar pola glob relatif repo (`*`, `**`, `?`). Write set kosong berarti `["**"]`
   (seluruh repo). Dua task yang write set-nya mungkin beririsan tidak pernah berjalan bersamaan. Sebelum
   gate, orchestrator menolak diff yang menyentuh file di luar write set task (gate gagal → REWORK dengan
   daftar file pelanggar).
5. **Dependensi:** PENDING → READY hanya kalau semua blocker DONE. Kalau blocker FAILED atau CANCELLED,
   task yang bergantung padanya → CANCELLED dengan alasan yang menyebut blocker. Kalau blocker NEEDS_HUMAN,
   task yang bergantung tetap PENDING dan run berhenti dengan status `needs_human`.
6. **Slot (ADR-0010):** `max_parallel` membatasi task yang sedang LEASED/RUNNING; semaphore per provider
   membungkus setiap request model; semaphore exec membatasi perintah bersamaan; slot merge = 1 (gate
   serial). Task yang menunggu gate tidak memegang slot worker.
7. **Interaksi pengguna diserialkan:** hanya satu pertanyaan (approval atau konfirmasi merge) yang tampil
   pada satu waktu, dan menunggu jawaban tidak memblokir event loop.
8. **Budget run:** jumlah token dari semua `usage.recorded` di run dibandingkan dengan budget run. Kalau
   terlampaui, scheduler berhenti me-lease task baru; task yang sedang berjalan diselesaikan, lalu run
   berhenti dengan status `needs_human` dan alasan budget.
9. `max_parallel` default **2** sampai benchmark M2-B memberi data (keputusan D11).

## Alternatif yang ditolak

- Planner LLM langsung mengeksekusi rencananya: pengguna kehilangan kesempatan meninjau graf dan biaya.
- YAML untuk plan: butuh dependensi baru dan lebih mudah salah ketik indentasi.
- Base commit tetap milik run: task dependen tidak melihat hasil blocker-nya dan konflik rebase lebih sering.
- Deteksi konflik write set yang presisi: deteksi konservatif (anggap beririsan kalau tidak bisa dibuktikan
  terpisah) lebih sederhana dan aman.

## Konsekuensi

- `Orchestrator.run()` menjadi scheduler asinkron; `advance()` tetap ada untuk satu langkah per task.
- `FakeWorker` harus bisa menulis file berbeda per task agar tes paralel tidak saling bentrok.
- Package baru `planning/` (format plan, IO TOML, planner LLM) berada di luar `core`.
- Mengubah graf hanya boleh saat run tidak sedang dieksekusi (aturan satu penulis, ADR-0002).
