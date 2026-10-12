LOCAL PROMPT ENGINEER — SMART GENERATION UPDATE

INSTALL (WINDOWS)
  1. Close the currently running Prompt Engineer app.
  2. Extract these files into your existing PromptEngineer folder,
     replacing BOTH prompt_engineer_gui.py and prompt_engineer.txt.
  3. Keep Lemonade Server running locally at http://localhost:13305.
  4. Run from PowerShell:
        py prompt_engineer_gui.py
     Or: python prompt_engineer_gui.py

REQUIRED
  - Python 3.9+ with tkinter; no additional pip packages.
  - Lemonade Server with either qwen3.5-4b-FLM or qwen3.5-2b-FLM installed.

WHAT CHANGED
  - Adaptive prompt generation rather than a mandatory long template.
  - Task selector: Auto, Studying, Coding, Research, Writing, Data analysis.
  - Detail selector: Auto, Concise, Detailed.
  - Better fidelity to language, constraints, scope and original meaning.
  - No invented parameters or sources; only necessary placeholders.
  - Default output is ONLY a usable prompt, without extra commentary.
  - 'Show full analysis': enable BEFORE generating/refining if you want the
    optional 'What changed' and 'Optional details' sections. Unchecking it
    later copies/shows just the cleaned prompt.
  - Refine applies feedback to the EXISTING optimized prompt and preserves
    unrelated details and any manual edits.
  - Prior history entries remain compatible, including ones with the old
    three-section Markdown output.
  - No changes to the local Lemonade connection or NPU model recipes.

FILES
  prompt_engineer_gui.py      Updated application
  prompt_engineer.txt         New, stronger system prompt (plain text)
  prompt_engineer_original.txt  Backup of the original Ollama-style TXT

HISTORY AND PRIVACY
  History remains in %APPDATA%\PromptEngineer\history.json and is not
  deleted or reset by this update.
  The application only sends requests to your local Lemonade API. It does
  not contact cloud AI providers.

SHORTCUTS
  Ctrl+Enter         Optimize
  Ctrl+Shift+Enter   Refine

SUGGESTED TESTS
  - Auto / Auto: "Explain what a capacitor does."
  - Studying / Concise: "Help me solve a torque problem, but don't give
    the final result unless I ask."
  - Coding / Detailed: "Debug this C function without changing its API."
  - Spanish input: "Explicame el momento angular sin resolverme los ejercicios."
  - Refine feedback: "Keep the same goal, but make it shorter."

LIMITATIONS
  The code, parser, persistence and GUI were tested locally with mocked
  server responses. Actual prompt quality and NPU speed must be tested
  with your installed model on Windows; improvements are not guaranteed
  for every request.
