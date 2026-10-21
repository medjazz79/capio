# Capio

**Translate. Understand. Listen.**
Your AI reading companion, right inside your book.

Translate books, understand difficult passages, and listen to translations
without leaving your reading experience. Use the floating translator to
translate selected text and images elsewhere on your screen.

## See Capio in action

<table>
  <tr>
    <td><img src="Capio-Preview-Reader-Russian.png" alt="Capio translating a book passage into Russian" width="100%"></td>
    <td><img src="Capio-Preview-Reader-Italian.png" alt="Capio translating a book passage into Italian" width="100%"></td>
  </tr>
  <tr>
    <td align="center"><sub>Read the Russian translation beside the original page.</sub></td>
    <td align="center"><sub>Switch to Italian while keeping your place.</sub></td>
  </tr>
  <tr>
    <td><img src="Capio-Preview-Reader-Spanish.png" alt="Capio translating a book passage into Spanish" width="100%"></td>
    <td><img src="Capio-Preview-Reader-Arabic.png" alt="Capio translating a book passage into Arabic" width="100%"></td>
  </tr>
  <tr>
    <td align="center"><sub>Follow the original as you read the Spanish translation.</sub></td>
    <td align="center"><sub>Read an Arabic translation alongside the book.</sub></td>
  </tr>
  <tr>
    <td><img src="Capio-Preview-Reader-Chat.png" alt="Ask the AI tutor a follow-up question about a selected passage" width="100%"></td>
    <td></td>
  </tr>
  <tr>
    <td align="center"><sub>Ask follow-up questions about the passage without leaving the reader.</sub></td>
    <td></td>
  </tr>
</table>

Capio keeps the translated passage beside the page, so you can continue
reading without switching applications.

## Floating translator: translate what is on your screen

<p align="center"><img src="Capio-Preview-Floating-Translator.png" alt="Capio's floating translator translating a Quanta Magazine article in a browser" width="720"></p>

Keep the floating button available over other applications, select an area,
then translate its text with OCR or send the image to a vision-capable model.

<p align="center"><img src="Capio-Preview-Floating-Translator-Article.png" alt="Capio translating a Quanta Magazine mathematics article beside the original page" width="720"></p>

## What you can do

- Translate a word, passage, or full page beside the original.
- Read scanned pages with OCR, or send an image to a vision-capable model.
- Ask AI to explain difficult passages.
- Listen to translations and follow word-by-word highlighting; resume where you
  stopped.
- With Edge TTS and Fast mode, Capio can prepare completed translations for
  quicker playback when you press **Read aloud**.
- Translate pages and continue through a book with automatic page-by-page
  reading.
- Use the floating translator outside the book reader.
- Open PDF, EPUB, DOCX, TXT, Markdown, HTML, XPS/OXPS, FB2, CBZ, and common
  image files.

For hands-free listening, start automatic page reading from the translation
window's page counter, or right-click a page and choose **Successive reading**
to open its translation and start reading automatically. In **Settings → Read
Aloud / TTS**, enable **Translate the next page while reading** to prepare each
following page in the background.
Capio waits five seconds after speech starts before requesting the next
translation, and waits for the current speech-generation task to finish before
sending that request. With Edge TTS and **Fast mode**, it also prepares the
next page's first audio segment after translation finishes, so playback can
start sooner when the current page ends.
Save the setting with **Save Changes**; it is remembered the next time you open
Capio. You can also enable **Stop successive reading after a set time** and
choose a duration in 15-minute steps up to four hours, or enter a custom
duration in minutes. Custom values are shown as hours and minutes (for example,
70 minutes becomes 1 h 10 min). When the limit is reached, Capio stops the
automatic sequence and current speech.

## I want to use Capio

1. Install **Python 3.10 or newer** on Windows.
2. Download and extract the project.
3. Double-click `run.bat` for the book reader or `run_float.bat` for the floating translator.

On first launch, Capio downloads the required libraries. Translation providers
may require an API key.

Your API keys and preferences are stored in your Windows user folder, outside
the project. If Capio cannot safely read its settings, it reports the problem
instead of starting with defaults that could overwrite your saved settings.

- [Installation and everyday use](USER_GUIDE.md)
- [Why Capio?](WHY_CAPIO.md)
- [Advanced Setup](ADVANCED_SETUP.md)

## I am a developer

- [Project layout, tests, performance notes, and packaging](DEVELOPMENT.md)
- [Provider and model integration details](ADVANCED_SETUP.md#translation-providers-api-keys-are-optional)

## License

Capio is licensed under the [MIT License](LICENSE).

## Search tags

**Reading:** #AIReadingAssistant #BookTranslation #PDFReader #EPUBReader
#DocumentTranslation #StudyTools

**Translation:** #AITranslation #OCR #VisionAI #ScreenTranslation
#FloatingTranslator #LanguageLearning

**Listening:** #TextToSpeech #TTS #ReadAloud #WordHighlighting

**Platform:** #Python #WindowsApp #OpenSource