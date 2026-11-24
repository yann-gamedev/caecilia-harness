# Architecture Decision Records

Satu file per keputusan. Nilai status: `Proposed` (jangan diimplementasikan), `Diterima`,
`Digantikan oleh ADR-NNNN`. ADR baru memakai nomor berikutnya dan heading yang sama dengan ADR yang ada.

| ADR | Judul | Status |
| --- | --- | --- |
| [0001](0001-python-asyncio-pydantic.md) | Python 3.12, asyncio, Pydantic v2 | Diterima |
| [0002](0002-sqlite-event-sourcing.md) | SQLite WAL, satu penulis, event-sourced | Diterima |
| [0003](0003-versioned-task-graph.md) | Task graph berversi dengan CAS | Diterima |
| [0004](0004-git-worktree-per-task.md) | Satu Git worktree per task yang menulis | Diterima |
| [0005](0005-serial-merge-queue-gate.md) | Merge queue serial sebagai integration gate | Diterima |
| [0006](0006-worker-capabilities.md) | Adapter worker mendeklarasikan kapabilitas | Diterima |
| [0007](0007-authentication.md) | Autentikasi hanya dengan API key dan OAuth terdokumentasi | Diterima |
| [0008](0008-secure-defaults.md) | Default aman: workspace-write + ask | Diterima |
| [0009](0009-out-of-process-plugins.md) | Plugin pihak ketiga berjalan di luar proses | Diterima |
| [0010](0010-slot-scope.md) | Slot konkurensi hanya dipegang selama operasi | Diterima |
| [0011](0011-durable-vs-ephemeral-events.md) | Event durable vs ephemeral | Diterima |
| [0012](0012-tui-dulu-gui-menyusul.md) | TUI dulu, GUI desktop menyusul sebagai frontend kedua | Diterima, diubah ADR-0015 |
| [0013](0013-rencana-run-dan-penjadwalan-paralel.md) | Rencana run dan penjadwalan paralel | Diterima |
| [0014](0014-konteks-bersama.md) | Konteks bersama: dokumen berversi, pin, dan invalidasi | Diterima |
| [0015](0015-perintah-global-dan-gui.md) | `caecilia` sebagai perintah global, sesi interaktif, dan GUI yang pasti dibangun | Diterima |
| 0016 | _(dipesan prompt 32: transport control API)_ | — |
| 0017 | _(dipesan prompt 34: teknologi GUI)_ | — |
| [0018](0018-worker-agent-cli-resmi.md) | Worker dari agent CLI resmi, termasuk dengan langganan pengguna | Diterima |
| [0019](0019-efisiensi-token.md) | Efisiensi token: koordinasi di kode, peran model, konteks sempit | Diterima |
