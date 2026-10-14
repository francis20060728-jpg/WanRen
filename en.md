# WanRen

**An AI content workbench for WeChat Official Accounts** — drop in your materials, let AI propose the outline first, edit in a true WYSIWYG canvas at WeChat's exact width, refine through chat, then sync drafts and publish in one click.

**English** | **[简体中文](README.md)** | **[Full user manual (Chinese)](docs/使用说明.md)**

---

## Highlights

- **Proposal before writing** — AI reads your materials, then proposes title, section-by-section outline and a recommended typesetting style. You confirm, tweak, or cancel before a single paragraph is written
- **True WYSIWYG** — the editor canvas is fixed at 677px (WeChat article width); fonts, figure captions, metric cards match the published article exactly. Ctrl+wheel scales the view without touching layout
- **8 typesetting style templates** — from Blue Magazine to Academic Frame; all render as WeChat-safe inline HTML (no flex/grid/gradients). Import custom styles via JSON, export and share
- **Paper-figure extraction** — embedded bitmaps and vector-drawn charts (clustered geometry, re-rendered at 200 dpi) both become captioned, numbered article assets from academic PDFs
- **Chat-driven editing** — AI edits the article block by block through visible tool calls; every round can be aborted, replayed with highlights, or rolled back
- **Writing-style templates** — 6 built-in voices (chat-friendly, deep-read, how-to, storytelling, sharp-comment, gentle-essay); import your own prose samples and the model learns the voice, never the topic
- **Aesthetic self-check** — after generation the rendered page is screenshotted and reviewed by a vision model; layout issues (image stacking, text walls) flow back as automatic fix instructions
- **Full WeChat pipeline** — multi-account, draft sync with automatic `draft/update`, import external drafts, scheduled publishing, status callbacks
- **Team & ops** — 5-role RBAC, AI cost tracking, workflow monitoring, audit log, 6 UI themes

## Quick Start (Docker)

Requires Docker 20+ with compose v2.

```bash
git clone https://github.com/francis20060728-jpg/WanRen.git
cd WanRen
cp .env.example .env
# recommended: set WANREN_APP_SECRET_KEY in .env  (openssl rand -hex 32)
docker compose up -d --build      # http://127.0.0.1:8000
```

Default dev account (only in `WANREN_AUTH_MODE=dev`): `admin@wanren.local` / `wanren-dev-admin`. For public deployments set `WANREN_AUTH_MODE=production` with a strong `WANREN_ADMIN_PASSWORD` and `WANREN_APP_SECRET_KEY` — production refuses to start without both.

By default `WANREN_PUBLISHER=mock`: a built-in mock account lets you try the entire flow (generation, editing, fake publishing) without any WeChat credentials.

> [!IMPORTANT]
> To connect a real Official Account you must BOTH set `WANREN_PUBLISHER=wechat` AND provide credentials — see "Connect WeChat" below. Leaving the publisher in mock mode silently fakes publishing.

Stop / upgrade (data lives in the `wanren-data` and `wanren-storage` volumes):

```bash
docker compose down          # stop, data preserved
docker compose up -d --build # upgrade
docker compose down -v       # DANGER: deletes all articles and assets
```

## Configure a model

Settings page → Main text model: choose `openai_compatible`, fill the API base (root URL only — no `/chat/completions` suffix), model name and API key, then hit "◎ Test connection". Keys are encrypted at rest and always shown masked. Vision: multimodal model (reuses main model when it accepts images) or the offline VidLens script. Everything can also be preset via `WANREN_TEXT_*` env vars; in-app settings take precedence.

## Connect WeChat

1. Set `WANREN_PUBLISHER=wechat` in `.env` and restart
2. In the [WeChat admin console](https://mp.weixin.qq.com): grab your **AppID** (Settings & Development → Basic Config), enable **AppSecret**, and **add your server's public IP to the IP whitelist** (same page) — otherwise every API call fails with `errcode 40164`
3. In WanRen's "Accounts" page add the account (name + AppID), then put its secret in `.env` as `WECHAT_APP_SECRET_<APPID_UPPERCASE>` and run "Check connection"
4. Personal-subscription accounts lack the publish API (`errcode 48001`): draft sync still works; do the final publish manually in the WeChat console
5. Optional callback: point `https://your-domain/wechat/<account-id>/callback` at the server config and set `WANREN_WECHAT_CALLBACK_TOKEN`

## First article in five steps

1. "＋ New chat" → describe the piece, drop materials (PDF/images/docs ride along), pick a style, send
2. Review the **architecture proposal** — confirm, or reply with changes ("make it 6 sections")
3. After generation you land in the editor+chat view; edit the article directly, or keep chatting refinements
4. "✓ Approve" → "▶ Sync draft" (a cover image is required — upload any picture in the chat first)
5. Preview in the WeChat console, then publish (or schedule from WanRen)

## Environment variables

See `.env.example` for the annotated full list. The critical ones:

| Variable | Purpose | Production |
|---|---|---|
| `WANREN_PUBLISHER` | `mock` / `wechat` | `wechat` |
| `WANREN_AUTH_MODE` | `dev` / `production` | `production` |
| `WANREN_ADMIN_PASSWORD` | bootstrap admin password | required, strong |
| `WANREN_APP_SECRET_KEY` | encrypts API keys stored in DB | `openssl rand -hex 32`, never change it |
| `WANREN_TEXT_MODEL` / `WANREN_TEXT_API_BASE` / `WANREN_TEXT_API_KEY` | main model | any OpenAI-compatible provider |
| `WECHAT_APP_SECRET_<APPID>` | per-account secret (kept out of the DB) | one per account |

## Production

```bash
docker compose -f docker-compose.yml -f compose.prod.yml --env-file .env up -d
```

Adds a read-only root filesystem, dropped capabilities, resource limits and production auth. Put HTTPS (Nginx/Caddy) in front — callbacks and login need it. Back up the `wanren-data` volume regularly.

## Tests

```bash
python -m pytest -q     # 197 tests: real HTTP layer, fake WeChat server, full publish pipeline
```

## Docs

- [使用手册](docs/使用说明.md) — full manual: every view, flow and caveats (Chinese)

## License

[万仞个人使用许可](./LICENSE) — personal and non-commercial use only; commercial use, paid resale and redistribution are prohibited.

---

<p align="center">WanRen · 2026</p>
