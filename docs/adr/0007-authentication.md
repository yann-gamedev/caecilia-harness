# ADR-0007: Autentikasi hanya dengan API key dan OAuth terdokumentasi

- Status: Diterima
- Tanggal: 2026-11-10

## Konteks

oh-my-pi meniru klien resmi untuk memakai kuota langganan konsumen. Halaman legal and compliance Claude Code menyatakan developer pihak ketiga tidak boleh menawarkan login Claude.ai atau merutekan permintaan lewat kredensial Free/Pro/Max, dan tidak boleh mengumpulkan atau menyimpan kredensial maupun token sesi Claude.ai; developer harus memakai API key (https://code.claude.com/docs/en/legal-and-compliance.md).

## Keputusan

Harness mengautentikasi ke provider model dengan API key yang disimpan di keychain OS (variabel env boleh untuk pengembangan), atau dengan OAuth hanya jika provider mendokumentasikannya untuk aplikasi pihak ketiga. Tidak ada spoofing klien. Agent eksternal (Claude Code, Codex) login sendiri lewat alur resminya; harness tidak pernah membaca atau menyalin kredensial mereka. Setiap adapter provider baru mencantumkan halaman terms provider di docstring modulnya.

## Alternatif yang ditolak

Login dengan langganan konsumen di dalam harness.

## Konsekuensi

Pengguna butuh API key sendiri. Claude Code yang di-spawn harus binary/SDK resmi yang tidak dimodifikasi.
