# Advanced Setup

Provider choices, model behavior, and configuration details for users who want
to customize Capio. For daily workflows, see the [User Guide](USER_GUIDE.md).

## Translation providers (API keys are optional)

1. **Gemini Flash:** get an API key from
   https://aistudio.google.com/apikey → ⚙ Settings → API Keys tab → paste → Save.
   Available models, quotas, and pricing depend on your Google account and plan.
2. **FreeLLMAPI:** a local router you
   run on your own machine. It stacks dozens of free providers behind one
   OpenAI-compatible `/v1` endpoint and **fails over between them on its own**,
   so a rate-limited upstream never reaches this app. Start the router, copy
   the unified key from its Keys page → ⚙ Settings → API Keys → FreeLLMAPI.
   The router URL is read from the router's own `config.json`, so the port is
   never guessed. In the picker you get `auto`, `auto:fast`, `auto:smart`,
   `auto:reliable`, `auto:balanced`, `fusion`, plus every model currently `ready`.
   Two things make it honest rather than a black box: the badge names the
   engine that actually answered (`🔀 groq/qwen/qwen3.8-27b`), read from the
   gateway's `X-Routed-Via` header, and a stream names it too — the badge comes
   from the streamed response, never a second request. `auto` also handles
   image translation, because the router routes images to a vision model
   itself. Measured here: `auto:fast` 2.4s, `auto` 4.5s.
3. **OpenCode bridge free, no key needed:** eight free models that answer
   through an in-process streaming bridge (`translators/opencode_bridge.py`),
   ordered fastest-first by measurement:
   `space-bunny-free` (3.4s) · `muse-spark-1.3-contributor-free` (4.3s) ·
   `mimo-v2.6-flash-free` (5.5s) · `fledge-alpha-free` (6.0s) ·
   `muse-spark-1.2-contributor-free` · `longcat-2.5-preview-free` ·
   `nemotron-3.5-lightning-free` · `nemotron-3-ultra-free`.
   The bridge uses streaming SSE plus the OpenCode client fingerprint
   (`stream:true` + shell/read tools + `opencode/` User-Agent + session id);
   without it the free tier answers 403. Muse-spark ids go through the
   `/responses` endpoint, the rest through `/chat/completions`. No API key
   field is offered and dead `-free` ids are not listed: a model that cannot
   answer only costs you a failed translation.
4. **Pick ONE free model** in ⚙ Settings → Translation tab, grouped so
   you always know what costs what:
   ✦ Gemini (free) · 🔀 FreeLLMAPI (your router) · 🌉 OpenCode bridge free
   (no key) · 🌐 Google Translate (unofficial, no key).
   Models that failed verification stay visible but disabled, so you can see
   why instead of discovering it mid-sentence.
5. **Custom providers (optional):** add an OpenAI-compatible endpoint in
   **Settings → Custom Providers**. After Base URL + API Key are entered, its
   `/v1/models` list is fetched automatically and appears in the Model menu.
   The **Reasoning** option controls reasoning on supported endpoints (for
   Ollama, it explicitly sends `think: true` or `think: false`); **Vision /
   Images** controls whether image selections are allowed and sent to that
   provider. Enable either only when the selected model and endpoint support
   it. Model menus show per-model capability labels; click the star beside a
   model to keep it in Favorites. Custom-provider model favorites are saved per
   provider, and provider panels remember whether they were collapsed.
   Each provider also has **Check Models**: it reads `/models`, sends one tiny
   test request per listed model (up to four at a time, with a 25-second
   timeout), and shows which ones answer right now. Rate limits, server errors,
   and timeouts are marked **RETRY / TEMPORARY**, not as confirmed model
   failures. Results also label models that provider metadata marks free;
   unknown pricing stays unverified. Testing paid models can use provider
   credits or incur charges, and a free label does not confirm that no
   subscription or payment card is required.
   Provider definitions are stored separately from general settings, so
   opening an older app build cannot erase them when it rewrites its settings.
   The legacy OpenAI-compatible
   fields still accept Base URL + Key + Model (e.g.
   `https://openrouter.ai/api/v1`).
6. **No API key:** Google Translate can also be selected explicitly. This uses
   Google's unofficial, keyless web endpoint; it may be rate-limited, changed,
   or unavailable, and selected text is sent to Google. If it fails, the app
   tries MyMemory and then the offline glossary for Arabic. The glossary is
   explicitly partial; if it contains no matching terms, the app reports that
   translation failed instead of presenting unchanged source text as a result.
   When another model is selected, the automatic fallback order is Google
   followed by MyMemory with language detection; putting your email in
   ⚙ Settings raises
   the MyMemory daily quota ~10x. If unreachable, the built-in offline glossary
   can translate some selected music terms (RU/EN/IT/DE/ES/FR); it is limited
   and cannot replace an AI model for general book translation.

Only the selected AI model is tried; a failed model is not replaced by another
AI model. If it fails, the app may use the separate free translation services
(Google or MyMemory), and the popup badge names that service rather than
claiming the selected model answered. **⚙ Settings → 🔌 Test engines** sends
one tiny probe to the **selected model only** — about a single round-trip — and
reports what that model just did. It does not sweep the fallback services;
those are exercised by a real translation, and the popup badge then names
whichever one answered. Gemini generation requests time out after 12 seconds;
connection timeouts are not retried, and the actual attempt count is included
in the error.

### Custom model instructions

In **⚙ Settings → APPLICATION → AI Model Instructions**, add extra guidance
for the AI translation models, such as preserving paragraph breaks or
keeping proper names unchanged. Built-in rules ask models to retain headings,
paragraphs, lists, and exactly one empty output line wherever the source has
a blank line between paragraphs; preserve subject-specific material in its
appropriate form, including code, equations, and musical examples where
present; omit running headers,
footers, marginal notes, footnotes, copyright lines, watermarks, and logos.
Keep only a clearly identified standalone page number, unchanged on the final
line in smaller text; it remains visible but is omitted from speech. Never
add translator notes,
labels, headers, or footers not present in the source; and write Arabic prose
RTL while keeping notation LTR.
For Arabic output from Latin-script sources, built-in rules also preserve the
original spelling in parentheses after the Arabic form of searchable proper
names (people, places, organizations, titles, brands, and notable named
events/concepts) on first mention only; later mentions use Arabic alone.
Ordinary words and generic terms are not copied into parentheses.
For clearly musical passages, complete note sequences are preserved in compact
LaTeX examples that act as visual references and are skipped by read-aloud.
This music-specific formatting does not apply to other subjects; code,
equations, and scientific notation keep the representation appropriate to
their field.
These instructions are appended to the built-in
translation rules and are saved with your settings. Leave the field empty to
use the built-in rules alone. Free online translators and the offline glossary
do not support custom model instructions. The field grows with the Settings
window, and a vertical splitter lets you resize it with the mouse.
Gemma 3:4B and Qwen3-VL:2B receive concise model-specific completeness
reminders to help them retain every paragraph and relevant example. Qwen3-VL's
reminder also tells it to translate image content rather than describe it;
other models keep their existing instructions unchanged.
For local Qwen3-VL:2B image translation, the app allows up to 8192 output
tokens; set Ollama's context length to at least 16K in Ollama Settings so the
image and full translation fit together.

## Settings layout and speed

⚙ Settings uses a sidebar, with **Translation** and **Custom Providers** at
the top, instead of one long list. The last opened section is saved as soon as
you select it, even if you later cancel; model favorites, provider model
choices, and collapsed provider panels are saved with settings.
Resize or move the window as needed; both its size and position are remembered
between opens.

**Performance → ⚡ Fast mode (default ON)** is the single switch for every
speedup; OFF restores classic behavior:
- **Live word-by-word display:** selected models stream each translation
  chunk, including OCR text and supported vision requests. This covers Gemini,
  FreeLLMAPI, OpenAI-compatible providers, OpenCode Zen/CLI text, and bridge
  models; image streaming is available only for models that accept images.
  Streaming improves time-to-first-text, not necessarily total completion
  time.
- **Router response window:** translation requests allow up to 120 seconds for
  a response, so a slow-starting local model is less likely to time out.
- **Fastest-first ordering:** bridge models are listed by measured speed.
- **Per-model effort:** LongCat defaults to minimal after paired speed checks;
  other models keep their existing effort because lower settings were slower
  or returned incomplete/unsuitable output in checks. Each model's preset
  remains editable.
- **Fast read-aloud:** playback can start from streamed audio before a complete
  MP3 segment is ready; segments still synthesize in parallel and completed
  audio is cached for fast repeats and restarts (Multilingual voice kept for
  multi-language work).
- **Font controls apply instantly:** Translation Font Size (10–48pt), Arabic
  Font, and Latin Font update the in-dialog preview and every open translation
  as they change — no Save needed; Cancel restores the previous typography.
  Arabic and Latin letters use their respective font menus in translated text
  and chat. Both apps (reader and floating translator) share one settings file
  and pick up each other's font changes within ~1.5s.
- **Arabic tashkeel:** In Settings → Application, enable **Add Arabic
  diacritics (tashkeel)** to ask the model to fully vocalize Arabic
  translations. It is off by default and applies to new translations whose
  target language is Arabic.

**⚙ Settings → 🔌 Test Selected Model** runs one tiny "OK" probe against the
selected model only (≤30 tokens), so the check takes seconds rather than a
full sample translation per engine.

## OCR engines

**OneOCR (Windows 11 native, default)** — the exact Snipping Tool engine.
`oneocr.dll` / `oneocr.onemodel` / `onnxruntime.dll` are **never bundled,
copied into the repo, or distributed**: they stay Microsoft property and are
used from your own installation (auto-located via the ScreenSketch package).
WindowsApps blocks external DLL loading, so on first use a working copy is
staged once into your own `%LOCALAPPDATA%\HarmonyTranslator\oneocr`
(same machine, same license; re-copied only on Snipping Tool updates).
Returns text + word boxes (image pixels) for overlays. OneOCR is the only OCR
provider; if Snipping Tool/OneOCR is unavailable, scanned-page OCR cannot run.