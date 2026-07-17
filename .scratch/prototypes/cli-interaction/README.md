# CLI Interaction Prototype (Throwaway)

Run from the repository root:

```powershell
python .scratch/prototypes/cli-interaction/cli.py
```

This throwaway prototype asks whether one compact terminal interaction model makes
task entry, streaming, plans, tool previews, permission prompts, interruption,
compaction, errors, completion, session listing, and resume understandable. It has
no model calls, tool execution, or persistence. Drive the scenarios and react to
what is visible and which actions are available.

For Issue #3's layout comparison, run:

```powershell
python .scratch/prototypes/cli-interaction/transcript_variants.py
```

The prototype now contains only the selected left-rail layout. Use `n` to
advance the sample: one user request owns the
entire Plan Mode workflow from inspection through patch and verification; a
later user request appears only after that workflow is complete. At the
permission checkpoint, use the numeric contract:

- `1` allows this exact Tool Call once.
- `2` allows this exact Tool, normalized resource set, and argument hash for the current Session.
- `3` denies the Tool Call and returns a denied Tool Result.
- `4` cancels the pending Tool Call/Turn.

Use `plan` to make the latest Plan snapshot visible and `r` to reset. Words and
aliases are invalid in this permission prompt; invalid input re-prompts without
creating a Permission Decision or Tool lifecycle event. This second prototype
is throwaway and has no persistence.

The A layout is the current preferred direction: You and Agent blocks are
separated by blank lines, Agent is labelled once per message block, Tool calls
use `[TOOL START]` and `[TOOL RESULT]`, and each Plan update is printed as its
own spaced block. The selected layout starts each conversation with `+ You`, keeps `|`
through the Agent stream, and ends the rail before the blank separator below
that Agent response; it has no Turn labels. A short Agent note flows directly
into its Tool marker, while a Tool result gets one blank line before the next
Agent message. Message content in the selected layout starts with `>` after the existing
rail; Tool and Plan lines do not. A Plan block has exactly one blank line below
it. After the transcript, a product-style separator shows context usage on the
left and slash-command hints on the right. When Plan Mode is on, only the
latest Plan snapshot appears immediately after the newest conversation content
and above the divider; Plan updates are not repeated in the transcript. Once
all Plan steps are complete, the bottom Plan snapshot is cleared. The
stage/status controls above the product area are explicitly prototype-only.
The status area follows the latest message and scrolls naturally with the
transcript; it is not pinned to the terminal window.
The prototype-only banner, layout name, stage/status line, and command footer
are explicitly marked because they are acceptance controls, not product UI.
