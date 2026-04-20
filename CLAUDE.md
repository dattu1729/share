# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

This repo runs [agentgateway](https://agentgateway.dev/) as a local proxy that intercepts GitHub Copilot traffic (chat, completions, agent mode) and ships traces to Jaeger via OTLP for observability.

## Stack

- **agentgateway v0.12.0** — LLM proxy (`ghcr.io/agentgateway/agentgateway:latest`)
- **Jaeger all-in-one** — trace storage and UI
- Config format: YAML, schema at `https://agentgateway.dev/schema/config`

## Running

```bash
# Start both services
docker compose up -d

# View agentgateway logs (request traces appear here)
docker compose logs agentgateway -f

# Restart after config change (no hot-reload in llm mode)
docker compose restart agentgateway

# Stop everything
docker compose down
```

## Key URLs

| Service                | URL                                          |
| ---------------------- | -------------------------------------------- |
| Jaeger UI              | http://localhost:16686                       |
| agentgateway LLM proxy | http://localhost:3000                        |
| agentgateway admin UI  | http://localhost:15000/ui (inside container) |

## Configuration

`config.yaml` is mounted read-only into the container at `/etc/agentgateway/config.yaml`. Agentgateway is started with `-f <path>` (file flag). The `-c` flag takes inline bytes — do not use it.

The config has two top-level sections:

- `config.tracing` — OTLP endpoint for Jaeger (uses `jaeger` docker service hostname)

## Environment

Do NOT use a static `GITHUB_TOKEN` / PAT. The requirement is to use the token from the editor's existing GitHub login (VS Code's authenticated session), not a separately managed credential.

## VS Code Integration

Add to VS Code `settings.json` (Cmd+Shift+P → "Open User Settings JSON"):

```json
{
  "github.copilot.advanced": {
    "debug.overrideProxyUrl": "http://localhost:3000"
  }
}
```

`debug.overrideProxyUrl` routes Copilot completions and chat to agentgateway. VS Code expects agentgateway to act as a full LLM provider (not a transparent proxy), which is why the `llm` config mode is required — it serves a proper `/v1/models` response that VS Code checks before sending chat requests.

### Open problem: editor token

The `llm` provider mode currently requires a static `GITHUB_TOKEN` in `.env` to authenticate with `api.githubcopilot.com`. This is **not the desired state** — the goal is to reuse the token from VS Code's active GitHub session so no separate credential is needed. A solution has not yet been found. Approaches to investigate:

- Extract the short-lived Copilot token from VS Code's secret storage / macOS Keychain and inject it dynamically
- Use the HTTP routing (`binds`) approach (no static token needed, VS Code passes its token through) — but this requires fixing the `/v1/models` 404 that causes VS Code to abandon chat requests
- Find a VS Code extension API or IPC mechanism that exposes the active session token to a local process

## Observability

Every proxied request generates a trace exported to Jaeger via OTLP gRPC on port 4317. In Jaeger UI, select service `agentgateway` to see spans. Each span includes `http.method`, `http.path`, `http.status_code`, and duration.

## Config Schema Reference

Key fields that required discovery:

- Backend TLS for HTTP routing: goes under `routes[].policies.backendTLS`, not on the backend object
- Host header rewrite: `routes[].policies.urlRewrite.authority.host`
- agentgateway CLI: `-f <file>` for file path, `-c <bytes>` for inline content
