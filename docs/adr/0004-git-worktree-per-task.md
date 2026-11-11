# ADR-0004: Satu Git worktree per task yang menulis

- Status: Diterima
- Tanggal: 2026-11-10

## Konteks

Penulis paralel butuh isolasi. oh-my-pi menunjukkan ruang masalah lengkapnya (file untracked, nested repo, backend copy-on-write, batas ukuran).

## Keputusan

Setiap task yang menulis mendapat `git worktree` yang dibuat dari base commit tercatat di bawah CAECILIA_HOME. Task read-only memakai checkout utama secara read-only. Harness menghitung diff sendiri, termasuk file untracked. Repo dengan submodule berjalan serial di MVP.

## Alternatif yang ditolak

Backend copy-on-write (APFS, Btrfs, reflink, overlayfs, ProjFS) seperti oh-my-pi.

## Konsekuensi

Lebih lambat di repo besar. Backend CoW bisa ditambahkan nanti di balik `WorkspaceBackend`.
