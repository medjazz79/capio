# Capio User Guide

This guide covers the everyday use of Capio. For model providers and optional
configuration, see [Advanced Setup](ADVANCED_SETUP.md). Developers can start
with [Development](DEVELOPMENT.md).


## دليل Capio السهل (بالعربية)

Capio يترجم الكلمات والصفحات أثناء قراءتك للكتاب، من غير ما تخرج من القارئ
وتنسخ النص إلى تطبيق آخر. هذا الدليل موجه للاستعمال اليومي، وما يحتاجش خبرة
في البرمجة.

## التثبيت والتشغيل

1. ثبّت Python على Windows إذا لم يكن موجودًا. يلزم Python 3.10 أو أحدث.
2. نزّل Capio وفكّ ضغط الملف في مجلد تختاره.
3. افتح المجلد واضغط مرتين على **`run.bat`** لفتح قارئ الكتب.
   لفتح المترجم العائم الذي يعمل فوق برامج أخرى، استعمل **`run_float.bat`**.
4. في أول تشغيل، سيقوم Capio بتنزيل المكتبات اللازمة تلقائيًا. يلزم اتصال
   بالإنترنت وقد يستغرق ذلك بضع دقائق. اترك نافذة الإعداد مفتوحة حتى يبدأ
   التطبيق.

## ترجمة كتاب

1. افتح كتابًا من زر فتح الملف.
2. اسحب بزر الفأرة الأيسر فوق كلمة أو جملة في الصفحة.
3. تظهر نافذة الترجمة قرب النص المحدد. يمكنك نسخ الترجمة أو طلب شرح لها.
4. لقراءة الصفحة التالية أو السابقة من نافذة الترجمة، استعمل أزرار التنقل
   الموجودة فيها.

إذا لم تظهر ترجمة، افتح **الإعدادات** واختر نموذج ترجمة. بعض النماذج تحتاج
مفتاحًا شخصيًا من مزوّد الخدمة، وبعض الخيارات قد تعمل من غير مفتاح. أضف المفتاح
داخل إعدادات Capio فقط؛ لا ترسله لأحد ولا تضعه في ملفات المشروع.

## الصور والصفحات الممسوحة

- إذا كانت الصفحة صورة ممسوحة ضوئيًا، استعمل وضع **OCR** لقراءة الكلمات من
  الصورة ثم ترجمتها.
- إذا أردت أن يفهم النموذج الصورة مباشرة، اختر وضع **Image**. يلزم لذلك نموذج
  يدعم الصور، وقد تختلف الخيارات حسب مزوّد الترجمة.
- يمكن تغيير وضع القراءة من إعدادات التطبيق أو قائمة المترجم العائم.

## الاستماع إلى الترجمة

اضغط **Read aloud** للاستماع. أثناء القراءة يتابع التحديد الكلمات المنطوقة.
اضغط **Stop** لإيقاف الصوت؛ عند تشغيل القراءة مرة أخرى تتابع من موضع التوقف.
تجد إعدادات الصوت والسرعة والصوت المختار داخل **Settings → Read Aloud / TTS**.
لبدء الاستماع إلى صفحات PDF بالتتابع، انقر عدّاد الصفحة في نافذة الترجمة، أو
انقر بالزر الأيمن على صفحة واختر **Successive reading** لفتح نافذة ترجمتها
وبدء القراءة التلقائية باستخدام خيارات القراءة المحفوظة. ويمكنك
تفعيل **Settings → Read Aloud / TTS → Translate the next page while reading**
لترجمة الصفحة التالية في الخلفية أثناء قراءة الصفحة الحالية، أو تركه معطلاً
للعودة إلى الانتظار حتى تنتهي الصفحة ثم ترجمتها. عند تفعيله، ينتظر التطبيق
5 ثوانٍ بعد بدء القراءة قبل طلب ترجمة الصفحة التالية، ولا يبدأ الطلب ما دام
عامل توليد الصوت للصفحة الحالية يعمل؛ وهذا يقلل تزامن طلبات الصوت والترجمة
إلى المزوّد نفسه. عند استخدام **Edge TTS** مع **Fast mode**، يجهّز التطبيق
أيضاً أول مقطع صوتي للصفحة التالية بعد اكتمال ترجمتها، لتبدأ القراءة أسرع عند
انتهاء الصفحة الحالية. ويمكنك تفعيل **Stop successive reading after a set time**
لتحديد مدة القراءة: اختر مدة بخطوات 15 دقيقة حتى 4 ساعات، أو اختر **Custom**
وأدخل المدة بالدقائق. يعرض التطبيق المدة المخصصة بالساعات والدقائق؛ فمثلاً
70 دقيقة تظهر **1 h 10 min**. عند انتهاء المدة يوقف التطبيق القراءة التتابعية
والصوت الجاري.

## إذا واجهت مشكلة

- إذا لم يفتح التطبيق، تأكد من تثبيت Python 3.10 أو أحدث ثم شغّل `run.bat`
  مرة أخرى.
- إذا توقف تنزيل المكتبات، تحقق من اتصال الإنترنت وأعد تشغيل ملف التشغيل.
- إذا لم تعمل الترجمة، افتح الإعدادات وتأكد من اختيار نموذج متاح وإضافة مفتاح
  المزوّد إذا كان مطلوبًا.
- الترجمة بالذكاء الاصطناعي ترسل النص أو الصورة المحددة إلى مزوّد النموذج الذي
  اخترته. لا تحدد معلومات خاصة إذا لم تكن مرتاحًا لإرسالها إلى ذلك المزوّد.

لشرح مزوّدي الترجمة والإعدادات المتقدمة، راجع
[دليل الإعداد المتقدم](ADVANCED_SETUP.md).


### Supported documents and page reading

Capio supports PDF, EPUB, DOCX, TXT, Markdown, HTML, XPS/OXPS, FB2, CBZ comics,
and common image formats (PNG, JPEG, BMP, GIF, TIFF, WebP, and SVG).
Non-PDF documents are rendered to temporary pages for reading and translation;
the original file is kept unchanged until saved. TXT and Markdown files can be
edited from **File → Edit source text** and saved in their original format with
**Save** (Ctrl+S); hold Ctrl and scroll over the editor to enlarge or reduce
the source font. The source editor uses a light background and opens at 18 px.
Markdown is edited as source, while the reader view renders its formatting.
Other non-PDF formats remain read-only; use **Save as PDF** to export a copy.
PDF editing and in-place saving continue to work as before.

### OCR and direct image translation

Works on **both** kinds of PDFs:
- **Text PDFs** — instant translation of the selected text.
- **Scanned-image PDFs** — OCR reads the selected area first, using the
  **native OneOCR engine** (Snipping Tool's) on Windows 11; OneOCR detects
  supported languages automatically, so there is no bundled third-party OCR
  fallback to install and no language list to configure.
- **Direct image translation** — choose **Settings → Application → Selection
  reading → Send selection image to vision model** to send the selected page
  area directly to the selected model, without local OCR. Gemini, OpenCode
  bridge/Zen, and OpenAI-compatible vision endpoints use their native image
  request formats. The selected model must support images; unsupported models
  report their provider error. Image mode always sends the rendered selection
  to the vision model, even when the PDF also has a text layer. OCR mode remains
  the default.
- **Hybrid scans** — some PDFs contain a scanned page image plus a small,
  incomplete hidden text layer. Full-page translation detects a large page
  image with sparse text and OCRs the rendered page instead of translating
  only that partial text layer.

## Use

- Use the two toolbar language selectors to choose a source and target. The
  source selector includes **Auto** (the default) plus Arabic, French, English,
  Russian, Italian, Spanish, and German; the target selector offers those
  seven languages. Choices are remembered. For example, **Auto → Italiano**
  detects the selected passage's language and translates it into Italian.
  Translation prompts for text and images explicitly require the selected
  target language even when the source is **Auto**. Longer model replies are
  checked; a mismatched reply gets one corrective request, and is rejected if
  it still fails the language check rather than being shown as a successful
  translation.
- Drag-select text in a document to translate it; **Ctrl+T** repeats the last
  selection. The selected target language is used for both text and OCR
  from scanned pages. Long passages are sent in larger, sentence-aware batches
  to reduce sequential model requests while preserving the full text.
- Use **Explain** in the translation popup (or right-click the floating bubble
  and choose **Explain**) to ask the selected model to add a concise explanation
  below each translation, in the target language. The setting is shared and
  remembered; switch it off for translation only.
- Use **Chat** beside Explain to ask follow-up questions about the selected
  passage and its translation. Replies use the selected model; the conversation stays with
  that popup and does not replace or edit the translation. The reply appears
  as it is generated. If the provider stops before confirming completion,
  Capio marks the received text as partial and shows the error instead of
  treating it as a complete answer. Send a message with **Send** or Ctrl+Enter.
  Use **»** beside the message field to change and remember reading speed.
  Drag the blue divider between the translation and chat to resize the chat
  area.
- Right-click inside the main window, a page, or a thumbnail to choose
  **Refresh** (reapply saved settings and redraw open pages and thumbnails in
  place) or **ReOpen** (save the open session and restart the app, loading
  changed Python code and saved settings while restoring books and reading
  positions). Unsaved PDF edits are handled by the normal
  save/discard/cancel prompt. A packaged executable must be rebuilt before
  source-code changes can appear in that executable.
- 📂 Open a book or document (one or many — each opens in its own **tab**; right-click
  a tab to 📌 pin it, close others, etc. Pinned tabs can't be closed
  by mistake). Pages are centered and new books fit with about a **1 cm
  margin on each side**). Drag and drop supported files onto the reader to open
  them. The last page and bookmarks for each book are remembered even after
  its tab is closed;
  all open tabs, pages, reading zooms, and per-book thumbnail zooms reopen
  next launch. The translation popup also remembers its size and position.
- The PDF canvas gets priority: thumbnails start hidden to maximize page
  width; use 🖼 **Pages** whenever you want the page overview.
- Drag the divider next to the thumbnails to resize their pane. Hold **Ctrl**
  and scroll over the thumbnails to zoom their images without changing pane
  width; thumbnails fit the pane with a 3 mm margin and 3 mm gaps, and the
  pane's scroll direction is reversed. Hold **Ctrl** and scroll over the PDF
  to zoom its pages.
- Books open progressively; thumbnails are populated only when the pages
  panel is shown, reducing work during PDF loading.
- Choose a built-in theme from the toolbar; the selection is remembered.
  Under **Settings → Appearance**, set **Color scheme** to **Follow system**,
  **Dark**, or **Light**. Enable **Match background around PDF pages to theme**
  to color the area around and between pages with the active theme; page content
  stays unchanged. The floating translator follows the same theme and
  color-scheme setting.
- No bars around the pages — messages flash briefly in the window title.
## Window layout

- Main window first opens at **1803×1044, centered**; later launches restore
  the last window size and position.
- Translation popup opens at outer frame **(514, 605, 1126, 326)** via Win32
  `SetWindowPos`; after moving or resizing, it remembers the user's chosen
  position and size.
- Zoom range **20%–1000%** (box, `⛶ Fit`, `Ctrl`+wheel, or buttons);
  last page zoom is remembered per tab.
- The **Fit** menu offers Actual Size, Fit Page, Fit Width, Fit Visible, and
  Zoom To. The thumbnails pane has a narrow vertical bookmark rail: **☆**
  saves/removes a bookmark for the current page, and **My bookmarks** switches
  to a pane of saved-page thumbnails. Clicking one navigates to that page and
  returns to the full thumbnail pane. Bookmarks are restored with their book
  on the next launch.
- Scroll PDF pages with the mouse wheel or the dedicated scrollbar on the
  reader's right edge; 🖼 toggles the separate thumbnails panel.
- Right-click a page thumbnail to translate the whole page or copy its rendered
  image to the clipboard. Use Shift+click to select a consecutive page range;
  right-click the selected range and choose **Extract pages** to save it as a
  separate PDF (the range includes both endpoint pages). Full-page translation
  uses OCR for scan-backed pages whose hidden text layer is sparse.
- Right-click directly on a large page in the PDF canvas for **Translate full
  page**, **Extract page**, **Delete page**, **Copy page image**, bookmark
  toggling, temporary left/right rotation for viewing, **Copy page text**, and
  **Refresh**. Temporary rotation does not modify the PDF file.
- Drag with the LEFT button over any sentence → translation popup above it.
  `Ctrl+T` repeats the last selection.
  Tip for scans: select a little bigger, with large print.
- Use **Previous page** and **Next page** in the translation popup to move the
  reader and translate the adjacent full page without closing the popup.
- 📌 Pin: keep a popup. 📋 Copy. **Read aloud** uses Microsoft Edge TTS by
  default; choose **Gemini TTS** in **Settings → Read Aloud / TTS** to use the
  configured Gemini API key and select from its 30 prebuilt voices. Gemini
  detects the spoken language from the text and supports Arabic, but does not
  provide word-level highlighting. Gemini API quotas and any charges depend on
  the current Google plan and pricing. By default Edge uses the selected voice
  (including Vivienne Multilingual). The live Edge voice catalog is available
  in both the translation popup's voice menu and **Settings → Read Aloud / TTS
  → Edge Voice**, limited to Arabic, French, Italian, English, German, Spanish,
  and Russian voices plus every Multilingual voice; use **Refresh** to reload
  the catalog.
  when reading Arabic text, Latin-script words and phrases are skipped by
  default so they cannot change the Arabic delivery or interrupt it with a
  player hand-off. Enable **Settings → Read Aloud / TTS → Latin Words** to
  include Latin-script words in speech. The selected voice handles the mixed
  text; the displayed translation is unchanged, and Edge word highlighting
  remains aligned to the original words.
  In a page-linked translation popup, click the **Page N / Total** indicator
  to start automatic reading from that page: the app translates each full page,
  reads it with the currently selected TTS settings, and continues in order.
  Click the indicator again to stop; at the end of the book it remains checked
  until clicked again. The page counter stays visible throughout.
  Arabic translations keep Western digits (`1944` stays `1944`); only the
  speech input uses Arabic digit forms and an explicit Arabic language tag so
  numbers are read in Arabic without changing the displayed text.
  Edge speech boundaries stay aligned with the original text, preserving word
  highlighting.
  Enable **Settings → Read Aloud / TTS → Automatic Voice** to use an Edge
  neural voice chosen for the target language instead.
  The reading-speed control uses a wide slider for 0.25x-2.00x adjustments,
  with one-hundredth precision, light quarter-step snapping, and quick-select
  speed presets. Its panel opens centered under the speed button; changing
  speed adjusts active audio playback directly without restarting speech.
  The speech engine, playback speed, automatic Edge voice, and Latin-word
  behavior are collected in **Settings → Read Aloud / TTS**. If the selected
  engine is unavailable or cannot synthesize speech, the popup reports the
  error instead of switching providers. Saving TTS settings refreshes the
  speech engine, voice, and speed controls in every open translation popup.
  Click a word in the translation to start reading from that point. During
  playback, clicking a word seeks within speech audio that is already prepared.
  Clicking whitespace in the translation toggles playback; stopping this way
  remembers the current word, and the next whitespace click resumes there.
  Dragging to select text does not start or seek speech.
  Edge segments
  stream to the player; Gemini segments are returned as WAV files, with
  following segments prepared in the background. Markdown in model
  output (such as **bold**,
  *italic*, lists, and headings) is rendered in the popup. Repeated whitespace
  and blank lines are normalized; punctuation and symbols are spoken as
  comma-like pauses, except wrappers and underscores in Latin phrases, which
  are normalized to spaces; numbers stay with the surrounding language run.
  Headings receive a short pause before the following text.
  Fenced notation remains visible but is skipped by read-aloud. Inline music
  notation wrapped in dollar signs remains visible without the delimiters and
  is skipped; ordinary prose such as `3f` stays unmarked and is read aloud.
  Read-aloud is optional; install Edge TTS with
  `.venv\Scripts\python.exe -m pip install -r setup\requirements-speech.txt`.
  With Arabic as the target and Latin-word reading enabled, Arabic and Latin
  text are synthesized together per bounded text chunk using the selected
  voice, avoiding a language-run hand-off between the scripts. All chunks keep
  the same Arabic SSML locale so the voice configuration does not switch as
  chunk contents change; Latin pronunciation depends on the selected voice.
  Continuous read-aloud starts bounded lookahead for up to two following
  segments alongside the initial request, then queues audio in order to reduce
  pauses at language transitions without reusing Edge TTS WebSocket connections.
  For timing diagnostics, set `CAPIO_TTS_TRACE=1` before launching; trace
  events are written to `%LOCALAPPDATA%\HarmonyTranslator\harmony_translator.log`.
  Playback samples record the player position, media/buffering state, and
  segment index every 250 ms to distinguish an audio-buffer stall from silence
  encoded into a synthesized segment.
  Speech requires an internet connection. Translation line spacing is adjustable
  in Settings
  (-50 to +50 px; 0 keeps natural spacing, negative values tighten lines and
  may cause overlap). ✕ / Esc:
  close. Set any Markdown formatting preference in **Application → AI Model
  Instructions**; Markdown in model output is rendered automatically, with
  Markdown headings, paragraphs, and musical examples consistently aligned at
  the normal reading edge in both the main reader and floating translator.
  Raw LaTeX artifacts trigger
  one corrective translation attempt and are rejected if they persist.
  Translation headings use a compact, consistent size hierarchy around the
  selected body font; standalone bold subheadings receive a modest size
  increase while inline bold remains emphasis only.

## Floating translator (other apps like Foxit Reader)

`run_float.bat` starts the floating translator without leaving a terminal
window open; its frameless button window has a taskbar entry and app icon.
Launching either Python entry point from File Explorer hides its standalone
console window. Running `python main.py` or `python main_02.py` from an existing
terminal leaves that terminal available for debugging.
The app provides ONE floating 🌐 button for other
programs (Foxit, browser...). Click it → drag-select any text on screen →
screenshot → OCR → the same translation popup (works even on scanned pages).
Drag the button anywhere (position remembered); right-click to toggle
**Explain**, choose **Image** or **OCR**, open settings, or quit. The translation
popup has independent source and target
language menus; the source defaults to **Auto** and can also be set explicitly.
Both menus include Arabic, French, English, Russian, Italian, Spanish, German;
changes are saved for the floating translator without changing the main
reader's language settings. It uses the same translation prompts, output-
language checks, notation handling, speech behavior, provider retries, and
fallback chain as the main reader. Esc/right-click cancels the selection; the
taskbar is never covered.
Drop a PDF onto the floating button to open it in the main PDF reader.
The bubble and tray menus also offer the same built-in themes as the reader;
the bubble follows the selected theme, while the translation popup keeps its
fixed colors. Theme and color-scheme changes made in either app are picked up
by the other. If OCR is unavailable, the floating translator explains that
OneOCR from Windows 11 Snipping Tool is required and offers Image mode as an
alternative.

Everything else the main reader remembers — window size/position, popup size,
zoom, pages panel on/off, books folder, last book + page — is listed under
**Window layout** above, along with the fact that the reader workspace resumes
with all open books and their tab order, active book, page and scroll position,
zoom, OCR language, pinned state, last selection, page-panel visibility,
splitter width, and window placement.
