# ADR-0001: Python 3.12, asyncio, Pydantic v2

- Status: Diterima
- Tanggal: 2026-11-10

## Konteks

Tidak ada kode referensi yang akan disalin, jadi pilihan bahasa bebas. Python punya Claude Agent SDK resmi, Textual untuk TUI, dan sudah dikuasai pemilik. Codex app-server dan ACP adalah protokol stdio yang bisa dipakai dari bahasa apa pun.

## Keputusan

Implementasi dengan Python 3.12, asyncio, dan model Pydantic v2 di setiap batas modul. Packaging dengan uv + hatchling.

## Alternatif yang ditolak

TypeScript, supaya dekat dengan oh-my-pi dan deepseek-harness.

## Konsekuensi

Tidak ada reuse kode dari repo referensi. Keamanan tipe bergantung pada mypy --strict.
