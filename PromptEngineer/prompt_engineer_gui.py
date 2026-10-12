"""An adaptive, dark-mode Prompt Engineer for Lemonade Server.

Requires Python 3.9+ and the tkinter module. No third-party packages.
Loads prompt_engineer.txt (plain text or legacy Ollama SYSTEM block) beside this file.
"""

import json
import os
import queue
import re
import threading
import tkinter as tk
import uuid
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from urllib import error, request

API_URL = "http://localhost:13305/v1/chat/completions"
PROMPT_FILE = Path(__file__).with_name("prompt_engineer.txt")
MODELS = ("qwen3.5-4b-FLM", "qwen3.5-2b-FLM")
MAX_HISTORY = 200

# Dark palette
BG = "#0e141c"
PANEL = "#19222e"
FIELD = "#111a25"
BORDER = "#293748"
TEXT = "#e8eef6"
MUTED = "#9eafc3"
ACCENT = "#64d3c4"
ACCENT_HOVER = "#8ae7d9"
SELECTION = "#285c68"

TASK_TYPES = ("Auto", "Studying", "Coding", "Research", "Writing", "Data analysis")
DETAIL_LEVELS = ("Auto", "Concise", "Detailed")

# Added to the user's configurable prompt to maintain compatibility with older
# prompt_engineer.txt files and to keep the API's output easy to copy.
QUALITY_INSTRUCTION = """Application rules (override any earlier conflicting formatting rules):
You are rewriting a prompt, not performing the underlying task.
Preserve the user's specific requirements without inventing constraints.
Use only as much structure as benefits this particular request.
Treat task type and detail level as rewriting guidance, not new requirements of the downstream task.
Do not produce a response to the underlying user question."""

TASK_GUIDANCE = {
    "Auto": "Infer the subject from the request. Avoid forced, generic instructions.",
    "Studying": "Prioritize accurate explanations and appropriate learning support. If the user asks for guidance instead of a solution, preserve that.",
    "Coding": "Prioritize technical precision, constraints, relevant code context, useful output, and appropriate tests; never invent a runtime or framework.",
    "Research": "Prioritize a clear research question, evidence quality, uncertainty, and source requirements only when useful. Do not claim that tools or browsing are available.",
    "Writing": "Prioritize original intent, audience, voice, tone, and deliverable format. Avoid generic filler.",
    "Data analysis": "Prioritize inputs, variables, calculations, units, expected deliverables, and validation. Never invent data or numeric findings.",
}

DETAIL_GUIDANCE = {
    "Auto": "Match the prompt's length to the complexity of the original request. For simple requests, keep it brief.",
    "Concise": "Produce the shortest complete, usable prompt. Retain all essential constraints; do not drop user requirements for brevity.",
    "Detailed": "Use helpful structure and explicit success criteria where justified. Do not pad, repeat, or invent requirements.",
}

REFINE_INSTRUCTION = """Refinement mode: the user supplies an original request,
a CURRENT optimized prompt, and an edit request. Revise the CURRENT prompt rather
than generating a different prompt from the original. Honor the feedback precisely,
keep unrelated requirements and manual edits, and output the COMPLETE revised prompt."""


def format_instruction(include_notes=False):
    if not include_notes:
        # Fewer scaffolding tokens make the smaller 2B model faster and less
        # likely to produce an explanation instead of the usable prompt.
        return """OUTPUT CONTRACT:
Return ONLY the complete rewritten prompt as plain text, ready to paste into another AI.
No heading, code fence enclosing the entire output, analysis, preface, or follow-up questions.
You MAY include code snippets or markdown within the prompt if the original request needs them.
Never answer the user's underlying request."""

    return """OUTPUT CONTRACT:
Return exactly this header and a single fenced text block containing the full optimized prompt:
#### OPTIMIZED PROMPT
```text
[complete optimized prompt here]
```
Do not include the square-bracket example literally. Do not add an introduction.
Do not answer the original request.
AFTER the closing fence, include these two short sections:
#### WHAT CHANGED
- At most three specific, useful edits (not generic praise).
#### OPTIONAL DETAILS
- Zero to two genuinely useful optional details the user could supply; write 'None' if none are needed.
These sections go OUTSIDE the code block and are not part of the copyable prompt.
If the optimized prompt itself requires fenced code blocks, use an outer fence longer
than any nested fence so the complete optimized prompt stays inside it."""


def history_path():
    """Keep personal history outside the source-code/project folder."""
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
        return base / "PromptEngineer" / "history.json"
    base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    return base / "prompt-engineer" / "history.json"


def load_system_prompt():
    raw = PROMPT_FILE.read_text(encoding="utf-8-sig")
    # Accept both the original Ollama Modelfile and a plain-text system prompt.
    match = re.search(r'SYSTEM\s+"""(.*?)"""', raw, flags=re.S)
    prompt = (match.group(1) if match else raw).strip()
    if not prompt:
        raise ValueError("System prompt file is empty")
    return prompt + "\n\n" + QUALITY_INSTRUCTION


def extract_optimized_prompt(text):
    """Extract clean copy-ready text from current OR legacy response formats."""
    text = (text or "").strip()
    # Optional internal parsing support if the model responds with XML-style tags.
    tag = re.search(r"<optimized_prompt>\s*(.*?)\s*</optimized_prompt>", text, re.I | re.S)
    if tag:
        return tag.group(1).strip()

    heading = re.search(r"(?im)^\s*#{0,6}\s*(?:OPTIMIZED PROMPT|PROMPT OPTIMIZADO)\s*:?[ \t]*$", text)
    section = text[heading.end():] if heading else text
    if heading:
        # Accept either three or more outer backticks, including fenced code
        # snippets inside a longer outer fence.
        block = re.search(r"(?m)^(`{3,})[^\n]*\n", section)
        if block:
            fence = block.group(1)
            closing = re.search(r"(?m)^" + re.escape(fence) + r"\s*$", section[block.end():])
            if closing:
                return section[block.end():block.end() + closing.start()].strip()
    else:
        # A model sometimes encloses the *whole* answer in one code fence.
        block = re.fullmatch(r"(`{3,})[^\n]*\n(.*)\n\1\s*", section, re.S)
        if block:
            return block.group(2).strip()
    # Model sometimes outputs no fence; stop at metadata headers.
    section = re.split(
        r"(?im)^\s*#{0,6}\s*(?:WHAT CHANGED|OPTIONAL DETAILS|"
        r"STRUCTURAL ENHANCEMENTS|SUGGESTED VARIABLES\s*/\s*CLARIFICATIONS|"
        r"MEJORAS|DETALLES OPCIONALES)\b",
        section,
    )[0]
    result = section.strip().strip('`').strip()
    if result.lower().startswith("optimized prompt:"):
        result = result.split(":", 1)[1].strip()
    return result or text


def replace_optimized_in_full(full_text, revised_prompt):
    """Keep manual edits synced when switching between prompt/analysis views."""
    full_text = full_text or ""
    tagged = re.search(r"(?is)(<optimized_prompt>\s*)(.*?)(\s*</optimized_prompt>)", full_text)
    if tagged:
        return full_text[:tagged.start(2)] + revised_prompt + full_text[tagged.end(2):]
    marked = re.search(
        r"(?ims)(^\s*#{0,6}\s*OPTIMIZED PROMPT\s*\n\s*(`{3,})[^\n]*\n)(.*?)(\n\2[ \t]*$)",
        full_text,
    )
    if marked:
        return full_text[:marked.start(3)] + revised_prompt + full_text[marked.end(3):]
    # No analysis sections; the raw response was already just the optimized text.
    return revised_prompt


def make_messages(system_prompt, user_prompt, previous_prompt=None, feedback=None,
                  task_type="Auto", detail_level="Auto", include_notes=False):
    if task_type not in TASK_GUIDANCE or detail_level not in DETAIL_GUIDANCE:
        raise ValueError("Unknown task type or detail level")
    settings = (
        f"TASK TYPE: {task_type}. {TASK_GUIDANCE[task_type]}\n"
        f"DETAIL LEVEL: {detail_level}. {DETAIL_GUIDANCE[detail_level]}\n\n"
        + format_instruction(include_notes)
    )
    system = system_prompt + "\n\n" + settings
    if previous_prompt is None:
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": "Rewrite this request as a prompt for another AI:\n\n" + user_prompt},
        ]
    if not feedback or not feedback.strip():
        raise ValueError("Refinement feedback is required")
    return [
        {"role": "system", "content": system + "\n\n" + REFINE_INSTRUCTION},
        {"role": "user", "content": (
            "ORIGINAL REQUEST (background; do not restart from this):\n"
            f"<original>\n{user_prompt}\n</original>\n\n"
            "CURRENT OPTIMIZED PROMPT (this is the text to revise):\n"
            f"<current>\n{previous_prompt}\n</current>\n\n"
            "USER FEEDBACK (apply exactly these changes):\n"
            f"<feedback>\n{feedback}\n</feedback>\n\n"
            "Return the complete REVISED prompt in the specified output format."
        )},
    ]


def generate_response(model, messages, timeout=180, opener=None):
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "top_p": 0.9,
        "stream": False,
    }
    req = request.Request(
        API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if opener is None:
        opener = request.urlopen
    with opener(req, timeout=timeout) as response:
        data = json.load(response)
    answer = data["choices"][0]["message"]["content"]
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("Lemonade returned an empty response.")
    return answer


class HistoryStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else history_path()
        self.entries = []
        self.warning = None
        if self.path.exists():
            try:
                content = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(content, list):
                    raise ValueError("History file should contain a list")
                self.entries = [item for item in content if isinstance(item, dict) and "id" in item]
                self.entries = self.entries[:MAX_HISTORY]
            except (OSError, ValueError, TypeError) as exc:
                # Keep the corrupt file untouched instead of overwriting it.
                self.warning = f"Could not read history: {exc}"
                self.path = self.path.with_name("history_recovered_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".json")

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(self.entries, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        finally:
            if tmp.exists():
                tmp.unlink()

    def add(self, entry):
        self.entries.insert(0, entry)
        self.entries = self.entries[:MAX_HISTORY]
        self.save()

    def get(self, entry_id):
        return next((entry for entry in self.entries if entry.get("id") == entry_id), None)

    def delete(self, entry_id):
        self.entries = [entry for entry in self.entries if entry.get("id") != entry_id]
        self.save()

    def clear(self):
        self.entries.clear()
        self.save()


def make_entry(original, model, answer, kind, feedback="", parent_id=None,
               task_type="Auto", detail_level="Auto", include_notes=False):
    return {
        "id": str(uuid.uuid4()),
        "created": datetime.now().astimezone().isoformat(timespec="seconds"),
        "kind": kind,
        "model": model,
        "original": original,
        "optimized": extract_optimized_prompt(answer),
        "full": answer,
        "feedback": feedback,
        "parent_id": parent_id,
        "task_type": task_type,
        "detail_level": detail_level,
        "include_notes": include_notes,
    }


class PromptEngineerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Prompt Engineer · Lemonade")
        self.root.geometry("1280x790")
        self.root.minsize(990, 600)
        self.root.configure(bg=BG)

        self.events = queue.Queue()
        self.history = HistoryStore()
        self.pending = False
        self.active_id = None
        self.display_full = False
        self.filtered_ids = []
        self.model = tk.StringVar(value=MODELS[0])
        self.task_type = tk.StringVar(value=TASK_TYPES[0])
        self.detail_level = tk.StringVar(value=DETAIL_LEVELS[0])
        self.show_full = tk.BooleanVar(value=False)
        self.search_text = tk.StringVar(value="")
        self.status = tk.StringVar(value="Ready · Lemonade Server must be running")
        self.result_info = tk.StringVar(value="Your optimized prompt will appear here")
        self.count_text = tk.StringVar(value="0 saved")

        self.setup_theme()
        self.build_ui()
        self.populate_history()
        self.update_actions()
        self.root.bind_all("<Control-Return>", self.on_optimize_shortcut)
        self.root.bind_all("<Control-Shift-Return>", self.on_refine_shortcut)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self.poll_events)
        if self.history.warning:
            self.status.set(self.history.warning + " · New history saved separately")
        self.input_text.focus_set()

    def setup_theme(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        # Also style the native drop-down list used by ttk.Combobox.
        self.root.option_add("*TCombobox*Listbox.background", FIELD)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", SELECTION)
        self.root.option_add("*TCombobox*Listbox.selectForeground", TEXT)
        style.configure("App.TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("App.TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 10))
        style.configure("Title.TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 20, "bold"))
        style.configure("Sub.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 10))
        style.configure("PanelTitle.TLabel", background=PANEL, foreground=TEXT, font=("Segoe UI", 11, "bold"))
        style.configure("PanelMuted.TLabel", background=PANEL, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("PanelInfo.TLabel", background=PANEL, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("Accent.TButton", background=ACCENT, foreground=BG,
                        padding=(17, 9), borderwidth=0, font=("Segoe UI", 10, "bold"))
        style.map("Accent.TButton",
                  background=[("disabled", BORDER), ("active", ACCENT_HOVER)],
                  foreground=[("disabled", MUTED), ("active", BG)])
        style.configure("Flat.TButton", background=BORDER, foreground=TEXT,
                        padding=(12, 8), borderwidth=0, font=("Segoe UI", 10))
        style.map("Flat.TButton",
                  background=[("active", SELECTION), ("disabled", PANEL)],
                  foreground=[("disabled", MUTED)])
        style.configure("Small.TButton", background=BORDER, foreground=TEXT,
                        padding=(9, 6), borderwidth=0, font=("Segoe UI", 9))
        style.map("Small.TButton", background=[("active", SELECTION), ("disabled", PANEL)],
                  foreground=[("disabled", MUTED)])
        style.configure("Dark.TCheckbutton", background=BG, foreground=TEXT,
                        font=("Segoe UI", 10), padding=(4, 4))
        style.map("Dark.TCheckbutton", background=[("active", BG)], foreground=[("disabled", MUTED)])
        style.configure("Dark.TCombobox", fieldbackground=FIELD, background=BORDER,
                        foreground=TEXT, bordercolor=BORDER, arrowcolor=TEXT, padding=5)
        style.map("Dark.TCombobox", fieldbackground=[("readonly", FIELD)],
                  foreground=[("readonly", TEXT)], selectbackground=[("readonly", FIELD)],
                  selectforeground=[("readonly", TEXT)])
        style.configure("Dark.Vertical.TScrollbar", background=BORDER, troughcolor=FIELD,
                        arrowcolor=MUTED, bordercolor=FIELD)

    def panel(self, parent):
        # Outer border and inner inset provide a consistent card appearance.
        border = tk.Frame(parent, bg=BORDER, bd=0)
        inner = ttk.Frame(border, style="Panel.TFrame", padding=14)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        return border, inner

    def editor(self, parent, height=9):
        shell = tk.Frame(parent, bg=BORDER, bd=0)
        text = tk.Text(shell, wrap="word", undo=True, font=("Consolas", 10),
                       bg=FIELD, fg=TEXT, insertbackground=ACCENT,
                       selectbackground=SELECTION, selectforeground=TEXT,
                       borderwidth=0, highlightthickness=0, relief="flat",
                       padx=12, pady=12, height=height, spacing1=2, spacing3=2)
        scroll = ttk.Scrollbar(shell, orient="vertical", command=text.yview,
                               style="Dark.Vertical.TScrollbar")
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y", padx=(0, 1), pady=1)
        text.pack(side="left", fill="both", expand=True, padx=(1, 0), pady=1)
        return shell, text

    def build_ui(self):
        wrapper = ttk.Frame(self.root, style="App.TFrame", padding=18)
        wrapper.pack(fill="both", expand=True)

        top = ttk.Frame(wrapper, style="App.TFrame")
        top.pack(fill="x", pady=(0, 15))
        ttk.Label(top, text="Prompt Engineer", style="Title.TLabel").pack(anchor="w")
        ttk.Label(top, text="Private, local prompt optimization · Powered by Lemonade",
                  style="Sub.TLabel").pack(anchor="w", pady=(2, 0))

        controls = ttk.Frame(wrapper, style="App.TFrame")
        controls.pack(fill="x", pady=(0, 13))
        ttk.Label(controls, text="Model", style="App.TLabel").pack(side="left")
        self.model_menu = ttk.Combobox(controls, values=MODELS, textvariable=self.model,
                                       width=19, state="readonly", style="Dark.TCombobox")
        self.model_menu.pack(side="left", padx=(7, 14))
        ttk.Label(controls, text="Task", style="App.TLabel").pack(side="left")
        self.task_menu = ttk.Combobox(controls, values=TASK_TYPES, textvariable=self.task_type,
                                      width=13, state="readonly", style="Dark.TCombobox")
        self.task_menu.pack(side="left", padx=(7, 14))
        ttk.Label(controls, text="Detail", style="App.TLabel").pack(side="left")
        self.detail_menu = ttk.Combobox(controls, values=DETAIL_LEVELS, textvariable=self.detail_level,
                                        width=9, state="readonly", style="Dark.TCombobox")
        self.detail_menu.pack(side="left", padx=(7, 10))
        ttk.Checkbutton(controls, text="Show full analysis", variable=self.show_full,
                        command=self.toggle_full, style="Dark.TCheckbutton").pack(side="left")
        self.optimize_btn = ttk.Button(controls, text="Optimize  ·  Ctrl+Enter",
                                       command=self.optimize, style="Accent.TButton")
        self.optimize_btn.pack(side="right")
        self.new_btn = ttk.Button(controls, text="New", command=self.new_prompt, style="Flat.TButton")
        self.new_btn.pack(side="right", padx=(0, 9))

        area = ttk.Frame(wrapper, style="App.TFrame")
        area.pack(fill="both", expand=True)
        area.columnconfigure(0, weight=0, minsize=225)
        area.columnconfigure(1, weight=1, uniform="editing")
        area.columnconfigure(2, weight=1, uniform="editing")
        area.rowconfigure(0, weight=1)

        # HISTORY ---------------------------------------------------------
        hb, history = self.panel(area)
        hb.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        header = ttk.Frame(history, style="Panel.TFrame")
        header.pack(fill="x", pady=(0, 10))
        ttk.Label(header, text="History", style="PanelTitle.TLabel").pack(side="left")
        ttk.Label(header, textvariable=self.count_text, style="PanelMuted.TLabel").pack(side="right")

        search = tk.Entry(history, textvariable=self.search_text, bg=FIELD, fg=TEXT,
                          insertbackground=TEXT, relief="flat", bd=0, highlightthickness=1,
                          highlightbackground=BORDER, highlightcolor=ACCENT,
                          font=("Segoe UI", 10))
        search.pack(fill="x", ipady=7, pady=(0, 10))
        self.search_text.trace_add("write", lambda *_: self.populate_history())

        list_shell = tk.Frame(history, bg=FIELD)
        list_shell.pack(fill="both", expand=True)
        self.history_list = tk.Listbox(list_shell, bg=FIELD, fg=TEXT,
                                       selectbackground=SELECTION, selectforeground=TEXT,
                                       activestyle="none", bd=0, highlightthickness=0,
                                       font=("Segoe UI", 10), exportselection=False)
        hscroll = ttk.Scrollbar(list_shell, orient="vertical", command=self.history_list.yview,
                                 style="Dark.Vertical.TScrollbar")
        self.history_list.configure(yscrollcommand=hscroll.set)
        hscroll.pack(side="right", fill="y")
        self.history_list.pack(side="left", fill="both", expand=True)
        self.history_list.bind("<<ListboxSelect>>", self.load_selected_history)

        hbuts = ttk.Frame(history, style="Panel.TFrame")
        hbuts.pack(fill="x", pady=(10, 0))
        self.delete_btn = ttk.Button(hbuts, text="Delete", style="Small.TButton",
                                      command=self.delete_selected)
        self.delete_btn.pack(side="left")
        self.clear_btn = ttk.Button(hbuts, text="Clear all", style="Small.TButton",
                                     command=self.clear_history)
        self.clear_btn.pack(side="right")
        ttk.Label(history, text="Stored only on this computer", style="PanelInfo.TLabel").pack(
            anchor="w", pady=(12, 0))

        # ORIGINAL ---------------------------------------------------------
        ib, source = self.panel(area)
        ib.grid(row=0, column=1, sticky="nsew", padx=(0, 10))
        ttk.Label(source, text="Your request", style="PanelTitle.TLabel").pack(anchor="w")
        ttk.Label(source, text="Write a rough prompt. Keep it natural.",
                  style="PanelMuted.TLabel").pack(anchor="w", pady=(3, 12))
        input_shell, self.input_text = self.editor(source, height=15)
        input_shell.pack(fill="both", expand=True)
        ttk.Label(source, text="Ctrl+Enter to optimize", style="PanelInfo.TLabel").pack(
            anchor="w", pady=(11, 0))

        # RESULT -----------------------------------------------------------
        ob, result = self.panel(area)
        ob.grid(row=0, column=2, sticky="nsew")
        ttk.Label(result, text="Optimized prompt", style="PanelTitle.TLabel").pack(anchor="w")
        ttk.Label(result, textvariable=self.result_info, style="PanelMuted.TLabel").pack(
            anchor="w", pady=(3, 12))
        output_shell, self.output_text = self.editor(result, height=7)
        output_shell.pack(fill="both", expand=True)
        rbuttons = ttk.Frame(result, style="Panel.TFrame")
        rbuttons.pack(fill="x", pady=(9, 12))
        self.copy_btn = ttk.Button(rbuttons, text="Copy prompt", command=self.copy_output,
                                    style="Flat.TButton")
        self.copy_btn.pack(side="left")
        self.save_btn = ttk.Button(rbuttons, text="Save TXT", command=self.save_output,
                                    style="Flat.TButton")
        self.save_btn.pack(side="left", padx=(8, 0))

        tk.Frame(result, bg=BORDER, height=1).pack(fill="x", pady=(1, 12))
        ttk.Label(result, text="Refine result", style="PanelTitle.TLabel").pack(anchor="w")
        ttk.Label(result, text="Describe what to change without starting over.",
                  style="PanelMuted.TLabel").pack(anchor="w", pady=(3, 8))
        feedback_shell, self.feedback_text = self.editor(result, height=2)
        feedback_shell.pack(fill="x")
        self.refine_btn = ttk.Button(result, text="Refine  ·  Ctrl+Shift+Enter",
                                      command=self.refine, style="Accent.TButton")
        self.refine_btn.pack(fill="x", pady=(10, 0))

        footer = ttk.Frame(wrapper, style="App.TFrame")
        footer.pack(fill="x", pady=(12, 0))
        ttk.Label(footer, textvariable=self.status, style="Sub.TLabel").pack(side="left")

    def get_input(self):
        return self.input_text.get("1.0", "end-1c").strip()

    def current_output(self):
        return self.output_text.get("1.0", "end-1c").strip()

    def current_optimized(self):
        value = self.current_output()
        return extract_optimized_prompt(value) if self.display_full else value

    def write_text(self, widget, value):
        widget.delete("1.0", "end")
        widget.insert("1.0", value)

    def persist_active_edits(self):
        entry = self.history.get(self.active_id) if self.active_id else None
        if entry is None:
            return
        field = "full" if self.display_full else "optimized"
        changed = entry.get(field, "") != self.current_output()
        input_changed = entry.get("original", "") != self.get_input()
        if changed or input_changed:
            entry[field] = self.current_output()
            entry["original"] = self.get_input()
            if changed:
                if field == "optimized":
                    entry["full"] = replace_optimized_in_full(entry.get("full", ""), entry["optimized"])
                else:
                    entry["optimized"] = extract_optimized_prompt(entry["full"])
            try:
                self.history.save()
            except OSError as exc:
                self.status.set(f"Couldn't save history changes: {exc}")

    def toggle_full(self):
        self.persist_active_edits()
        self.display_full = self.show_full.get()
        entry = self.history.get(self.active_id) if self.active_id else None
        if entry:
            self.write_text(self.output_text, entry.get("full" if self.display_full else "optimized", ""))

    def populate_history(self, selected_id=None):
        search = self.search_text.get().strip().casefold()
        entries = [e for e in self.history.entries if
                   search in e.get("original", "").casefold() or
                   search in e.get("optimized", "").casefold() or
                   search in e.get("feedback", "").casefold()]
        self.filtered_ids = [e["id"] for e in entries]
        self.history_list.delete(0, "end")
        for entry in entries:
            try:
                stamp = datetime.fromisoformat(entry.get("created", "")).strftime("%m/%d %H:%M")
            except ValueError:
                stamp = "Unknown"
            title = " ".join(entry.get("original", "").split())[:43] or "Untitled prompt"
            action = "↻" if entry.get("kind") == "refine" else "•"
            self.history_list.insert("end", f"{action} {stamp}  {title}")
        self.count_text.set(f"{len(self.history.entries)} saved")
        target_id = selected_id if selected_id is not None else self.active_id
        if target_id in self.filtered_ids:
            index = self.filtered_ids.index(target_id)
            self.history_list.selection_set(index)
            self.history_list.see(index)
        if hasattr(self, "delete_btn"):
            self.update_actions()

    def load_selected_history(self, _event=None):
        if self.pending:
            return
        selected = self.history_list.curselection()
        if not selected:
            return
        entry_id = self.filtered_ids[selected[0]]
        if entry_id == self.active_id:
            return
        self.persist_active_edits()
        entry = self.history.get(entry_id)
        if not entry:
            return
        self.active_id = entry_id
        if entry.get("model") in MODELS:
            self.model.set(entry["model"])
        self.task_type.set(entry.get("task_type", "Auto") if entry.get("task_type") in TASK_TYPES else "Auto")
        self.detail_level.set(entry.get("detail_level", "Auto") if entry.get("detail_level") in DETAIL_LEVELS else "Auto")
        self.write_text(self.input_text, entry.get("original", ""))
        self.write_text(self.output_text, entry.get("full" if self.display_full else "optimized", ""))
        self.write_text(self.feedback_text, entry.get("feedback", ""))
        self.result_info.set(f"{entry.get('model', 'Unknown model')} · {entry.get('kind', 'optimize').title()}")
        self.status.set("Loaded from history · You can edit or refine it")
        self.update_actions()

    def new_prompt(self):
        if self.pending:
            return
        self.persist_active_edits()
        self.active_id = None
        self.history_list.selection_clear(0, "end")
        self.write_text(self.input_text, "")
        self.write_text(self.output_text, "")
        self.write_text(self.feedback_text, "")
        self.result_info.set("Your optimized prompt will appear here")
        self.status.set("New prompt · Ready")
        self.update_actions()
        self.input_text.focus_set()

    def update_actions(self):
        busy = self.pending
        self.optimize_btn.configure(state="disabled" if busy else "normal")
        self.new_btn.configure(state="disabled" if busy else "normal")
        can_refine = bool(self.active_id and self.current_optimized())
        self.refine_btn.configure(state="normal" if can_refine and not busy else "disabled")
        self.copy_btn.configure(state="normal" if self.current_output() else "disabled")
        self.save_btn.configure(state="normal" if self.current_output() else "disabled")
        self.delete_btn.configure(state="normal" if self.history_list.curselection() and not busy else "disabled")
        self.clear_btn.configure(state="normal" if self.history.entries and not busy else "disabled")

    def optimize(self):
        if self.pending:
            return
        user_prompt = self.get_input()
        if not user_prompt:
            messagebox.showinfo("Missing prompt", "Enter a rough prompt first.")
            return
        self.launch("optimize", user_prompt, "", None, None)

    def refine(self):
        if self.pending:
            return
        previous = self.current_optimized()
        feedback = self.feedback_text.get("1.0", "end-1c").strip()
        if not previous:
            messagebox.showinfo("Nothing to refine", "Generate or load a prompt first.")
            return
        if not feedback:
            messagebox.showinfo("Missing feedback", "Explain what you'd like to change.")
            self.feedback_text.focus_set()
            return
        self.launch("refine", self.get_input(), feedback, previous, self.active_id)

    def launch(self, kind, user_prompt, feedback, previous_prompt, parent_id):
        try:
            system = load_system_prompt()
        except (OSError, UnicodeError) as exc:
            messagebox.showerror("System prompt not found",
                                 f"Could not open {PROMPT_FILE.name}:\n{exc}")
            return
        self.persist_active_edits()
        model = self.model.get()
        task_type = self.task_type.get()
        detail_level = self.detail_level.get()
        include_notes = self.show_full.get()
        messages = make_messages(system, user_prompt, previous_prompt, feedback,
                                 task_type=task_type, detail_level=detail_level,
                                 include_notes=include_notes)
        self.pending = True
        self.status.set(f"{'Refining' if kind == 'refine' else 'Optimizing'} with {model}…")
        self.update_actions()
        meta = {"model": model, "original": user_prompt, "kind": kind,
                "feedback": feedback, "parent_id": parent_id,
                "task_type": task_type, "detail_level": detail_level,
                "include_notes": include_notes}
        threading.Thread(target=self.worker, args=(messages, meta), daemon=True).start()

    def worker(self, messages, meta):
        try:
            answer = generate_response(meta["model"], messages)
            self.events.put(("ok", answer, meta))
        except error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")[:700]
            self.events.put(("error", f"HTTP {exc.code}: {details}", meta))
        except error.URLError as exc:
            self.events.put(("error", f"Could not connect to Lemonade at {API_URL}.\n{exc}", meta))
        except Exception as exc:
            self.events.put(("error", str(exc), meta))

    def poll_events(self):
        try:
            while True:
                kind, value, meta = self.events.get_nowait()
                self.pending = False
                if kind == "ok":
                    entry = make_entry(meta["original"], meta["model"], value,
                                       meta["kind"], meta["feedback"], meta["parent_id"],
                                       task_type=meta["task_type"],
                                       detail_level=meta["detail_level"],
                                       include_notes=meta["include_notes"])
                    try:
                        self.history.add(entry)
                        self.status.set("Done · Saved to history · Ready to copy or refine")
                    except OSError as exc:
                        # HistoryStore.add already inserted the in-memory entry.
                        self.status.set(f"Generated · History could not be saved: {exc}")
                    self.active_id = entry["id"]
                    self.write_text(self.input_text, entry["original"])
                    self.write_text(self.output_text,
                                    entry["full" if self.display_full else "optimized"])
                    self.write_text(self.feedback_text, "")
                    self.result_info.set(f"{entry['model']} · {entry['kind'].title()}")
                    self.populate_history(selected_id=self.active_id)
                else:
                    self.status.set("Generation failed · Check Lemonade and the model ID")
                    messagebox.showerror("Lemonade request failed", value)
                self.update_actions()
        except queue.Empty:
            pass
        self.root.after(100, self.poll_events)

    def on_optimize_shortcut(self, _event):
        self.optimize()
        return "break"

    def on_refine_shortcut(self, _event):
        self.refine()
        return "break"

    def copy_output(self):
        # Copy only the usable optimized prompt, even while full analysis is visible.
        text = self.current_optimized()
        if not text:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.status.set("Copied optimized prompt to clipboard")

    def save_output(self):
        text = self.current_output()
        if not text:
            return
        target = filedialog.asksaveasfilename(
            defaultextension=".txt", filetypes=[("Text files", "*.txt")],
            initialfile="optimized_prompt.txt",
        )
        if target:
            try:
                Path(target).write_text(text, encoding="utf-8")
                self.status.set(f"Saved to {target}")
            except OSError as exc:
                messagebox.showerror("Couldn't save output", str(exc))

    def delete_selected(self):
        if self.pending:
            return
        selected = self.history_list.curselection()
        if not selected:
            return
        entry_id = self.filtered_ids[selected[0]]
        if not messagebox.askyesno("Delete history entry", "Delete this saved prompt?"):
            return
        try:
            self.history.delete(entry_id)
        except OSError as exc:
            messagebox.showerror("Couldn't delete entry", str(exc))
            return
        if self.active_id == entry_id:
            self.active_id = None
            self.write_text(self.input_text, "")
            self.write_text(self.output_text, "")
            self.write_text(self.feedback_text, "")
            self.result_info.set("Your optimized prompt will appear here")
        self.populate_history()
        self.status.set("History entry deleted")

    def clear_history(self):
        if self.pending or not self.history.entries:
            return
        if not messagebox.askyesno("Clear history", "Permanently delete all saved prompt history?"):
            return
        try:
            self.history.clear()
        except OSError as exc:
            messagebox.showerror("Couldn't clear history", str(exc))
            return
        self.active_id = None
        self.write_text(self.input_text, "")
        self.write_text(self.output_text, "")
        self.write_text(self.feedback_text, "")
        self.result_info.set("Your optimized prompt will appear here")
        self.populate_history()
        self.status.set("History cleared")

    def on_close(self):
        self.persist_active_edits()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    PromptEngineerApp(root)
    root.mainloop()
