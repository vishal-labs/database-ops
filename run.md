# Local Setup Guide: Agentic Postgres Analytics Dashboard

This document provides the end-to-end process for configuring, starting, and running the Agentic Postgres Analytics Dashboard on your local machine using Docker, OpenCode, and Gemini 2.5 Flash.

## 1. Prerequisites

Ensure the following tools are installed on your system before beginning:
*   **Docker Desktop** (or Docker Engine) running in the background.
*   **Node.js** (v18 or newer).
*   **OpenCode CLI** installed globally (e.g., via Homebrew: `brew install sst/tap/opencode`).
*   A valid **Google Gemini API Key**.

## 2. Environment Configuration

The OpenCode agent requires explicit configuration to route requests to Gemini instead of its default providers. 

Open your terminal and export your API key and the target model identifier. If you want these to persist across sessions, add these lines to your `~/.zshrc` or `~/.bashrc` file.

```bash
export GEMINI_API_KEY="your-actual-gemini-key"
export GOOGLE_API_KEY="your-actual-gemini-key"
export OPENCODE_MODEL="gemini/gemini-2.5-flash"