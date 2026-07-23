## Project Intake Tool Spec

Runs a deterministic, multi-turn, one-question-at-a-time interview to
build a project brief when the user wants to kick off new work (e.g.
"vamos começar um novo projeto"). Unlike a normal builtin tool, this one
is backed by a **deterministic pre-planner gate** so that state survives
across turns without depending on the chat model's own memory of the
conversation.

### Why a gate, not just a tool

A single tool call cannot hold multi-turn state on its own — the
planner/router run fresh every turn and have no guarantee of recognising
"we are mid-interview" the way a small local model might miss it. The
fix mirrors `recall_gate.spec.md`: a cheap, no-LLM, pre-flight check that
runs **before** the planner, and when it fires, bypasses planner/router
entirely for that turn. Determinism lives in the code path, not in the
model's discipline.

This now covers every stage of the flow deterministically, including
*starting* a session (see "Trigger detection" below) — closing what was
the last LLM-dependent gap: voice testing observed the small chat model
sometimes talking about starting a project instead of actually invoking
`projectIntake` after the router selected it, so tool-calling reliability
alone was not enough even for that one decision.

### Database

```sql
project_intake_sessions (
  id INTEGER PRIMARY KEY,
  conversation_id TEXT NOT NULL DEFAULT 'default',  -- ties session to the active conversation
  project_name TEXT,                 -- filled once known, may start NULL
  project_type TEXT,                 -- template key once resolved, else NULL
  status TEXT NOT NULL,              -- 'awaiting_type' | 'in_progress' | 'completed'
  questions_json TEXT,               -- frozen question list once type resolves
  answers_json TEXT NOT NULL DEFAULT '[]',
  current_index INTEGER NOT NULL DEFAULT 0,
  abandoned INTEGER NOT NULL DEFAULT 0,  -- see "Abandoning an in-progress interview"
  obsidian_saved INTEGER NOT NULL DEFAULT 0,  -- see "Retrying a failed Obsidian save"
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
)
```

A pre-existing on-disk database (one created before `obsidian_saved`
existed) gets the column added via an `ALTER TABLE` migration in
`Database._migrate_schema`, guarded by `PRAGMA table_info` so it's
idempotent on every startup — `CREATE TABLE IF NOT EXISTS` alone only
covers brand-new databases.

One active (`status != 'completed'`) row per `conversation_id` at a time.
Starting a new project while one is already in progress is handled as a
tool-level decision (see "Starting" below), not a DB constraint — this
keeps the failure mode a friendly reply instead of a write error.

`conversation_id` defaults to the constant `'default'` and is not
currently varied: the app runs a single global dialogue session (one
`DialogueMemory` instance, not multiplexed by conversation ID), so "one
active row per conversation" collapses to "one active row, full stop"
in practice. The column is kept for forward compatibility if the app
ever multiplexes sessions.

### Templates

Templates live in `project_templates.json` (project-config territory,
alongside `config.json`, not hardcoded in Python) so new categories or
edited questions never require a code change:

```json
{
  "<template_key>": {
    "label": "Human-readable name",
    "keywords": ["match", "terms", "lowercased"],
    "questions": ["Question 1", "Question 2", "..."]
  }
}
```

`other` is required and MUST have an empty `keywords` list — it is the
fallback template, never matched on purpose, always available as a
catch-all.

### Public schema

Single optional string input, following the same direct-exec-friendly
shape as `log_meal`:

```json
{
  "type": "object",
  "properties": {
    "input": {
      "type": "string",
      "description": "What the user said — either a request to start a new project, an answer to the pending intake question, or the project type when asked"
    }
  }
}
```

The tool infers what to do from **database state**, not from the
argument's shape. This is what makes it safe for the planner to call
blindly with `input='<redacted user text>'` every relevant turn.

### The gate (engine-level, runs before the planner)

In `run_reply_engine`, immediately after redaction and before
`plan_query()`:

```
session = get_active_intake_session(conversation_id)
if session is not None:
    force tool_call = {"name": "projectIntake", "arguments": {"input": redacted_text}}
    skip planner, tool router, and memory enrichment for this turn
```

This is a hard override, same tier as the recall gate — cheap, pure
lookup, fail-open (`get_active_intake_session` returning `None` on any
DB error just lets the turn proceed normally). Because it runs before
the planner, an in-progress interview can never be derailed by the
planner deciding to do something else with the turn.

### Trigger detection (starting a new session)

When the gate finds **no** active session, a second deterministic check
runs — `is_start_trigger_phrase(redacted_text)` — right after the retry-
save check (see "Retrying a failed Obsidian save"), still before the
planner/router:

```
if get_gated_session() is None:
    if maybe_retry_obsidian_save(...) matched: handled, return
    if is_start_trigger_phrase(redacted_text):
        force tool_call = {"name": "projectIntake", "arguments": {"input": redacted_text}}
        skip planner, tool router, and memory enrichment for this turn
```

This used to be the one point in the flow where tool selection depended
on normal (LLM) routing: the tool's one-line catalogue description —

> "Call when the user wants to start a new project, kick off a new
> piece of work, or explicitly says something like 'let's start a new
> project' / 'vamos começar um novo projeto'."

— relies on the router selecting `projectIntake` *and* the chat model
actually invoking it. In voice testing the second half proved
unreliable: the small model sometimes talked about starting a project
("Ok, vamos começar! Que tipo de projeto...") without ever emitting a
tool call, so no session was created and the next turn had nothing to
gate on. `is_start_trigger_phrase` closes that gap the same way the
restart-trigger and retry-save checks already do: NFKD-strip-accents +
casefold normalise, then substring-match against a PT/EN keyword list
(`_START_TRIGGER_PHRASES`) covering phrasing like "vamos começar um novo
projeto" / "começar um novo projeto" / "let's start a new project" /
"start a new project" / "begin a new project". Bare "novo projeto" /
"outro projeto" are also recognised, but only by **exact match** on the
whole (normalised) utterance rather than substring containment — a
minimal voice command like "Novo projeto." should work, but the same two
words appearing inside an unrelated sentence (e.g. "o novo projeto da
câmara municipal vai custar milhões") must not hijack the turn. This is
the same "generic marker only counts combined with something specific"
guard `_is_retry_save_phrase` uses for its "try again" marker (there,
paired with an explicit Obsidian mention; here, paired with being the
entire utterance instead of a second keyword).

Normal (LLM) routing is still the fallback when neither deterministic
check fires — the tool remains in the catalogue and the router/planner
can still select it for phrasing this list doesn't cover — but starting
a session no longer *depends* on that path succeeding.

### Flow

1. **No active session, trigger phrase recognised** → `run()` creates a
   row with `status='awaiting_type'`, `questions_json=NULL`. Returns:
   > "Que tipo de projeto é este? Site, App, Campanha de Marketing,
   > Marca/Identidade Visual, ou Outro?"

2. **`status='awaiting_type'`** → deterministic keyword match: NFKD-normalise
   `input` (decompose accented characters, strip the combining marks) then
   casefold, check substring overlap against each
   template's `keywords` list (first match wins; longer keyword lists
   checked before shorter ones to prefer specific over generic).
   - Match found → freeze that template's `questions` into
     `questions_json`, set `project_type`, `status='in_progress'`,
     `current_index=0`. Return question 1.
   - No match → fall back to `other`, same transition. Never re-asks
     the type question — an unrecognised answer must not stall the
     flow.

3. **`status='in_progress'`** → append `input` to `answers_json` at
   `current_index`, increment `current_index`.
   - More questions remain → return the next question verbatim from
     `questions_json`.
   - Last question just answered → set `status='completed'`, compile
     the brief (template label + every question/answer pair, ordered),
     persist it (see "On completion"), return the compiled summary.

4. **No active session, no trigger phrase** → tool is simply not
   selected this turn; irrelevant to normal conversation.

### One question per turn, by construction

The tool returns exactly one question's text per call — never the full
list. The chat model's only job is to relay that string. This makes
"ask one thing and stop" a property of the data returned, not a rule the
model has to remember to follow.

### On completion — write to Obsidian, do NOT auto-delegate yet

Intake ending and development starting are two separate, user-gated
moments — completion only **saves the plan**; it does not hand anything
to Antigravity. That happens later, on its own explicit trigger (see
"Starting development" below), so the user reviews/edits the plan in
the vault before any agent work is dispatched.

- The compiled brief is written as a note via the Obsidian MCP
  (server key `obsidian`, respecting the vault's existing PARA structure
  and note-format conventions):
  - Path: the relevant project folder if one already exists for this
    business/client, else a new folder created under the vault's
    project-numbering convention (same pass that creates the folder
    also creates/updates its `<Folder Name>.md` index, per the vault's
    own rules).
  - Filename: the project name (slugified) if given during intake, else
    `<project_type> - <YYYY-MM-DD>`.
  - Frontmatter: `status: active`, `project: <slug>`, `type: plan`.
  - Body: template label as a heading, then every question/answer pair
    in order, then a `## Status` line: `Plano criado, desenvolvimento
    ainda não iniciado.`
- `project_intake_sessions.answers_json` also keeps a local copy
  (already written during the flow) purely as a fail-open cache — if
  the Obsidian MCP write fails, the tool reports the failure honestly
  ("brief guardado localmente, mas falhou a gravação no Obsidian —
  tenta 'grava o plano' outra vez mais tarde") rather than claiming
  success it can't back up. This is the same rule as everywhere else in
  the system: never confirm an action the tool result didn't confirm.
- `obsidian_saved` is set to `1` only once `write_brief_to_obsidian`
  actually confirms the write; it stays `0` if the write fails or was
  never attempted. This is what makes the retry path below possible —
  see "Retrying a failed Obsidian save".
- The `project_intake_sessions` row stays `status='completed'` for
  history; it is not deleted. A later "vamos começar um novo projeto"
  always opens a fresh row.

### Retrying a failed Obsidian save

Once a session is `status='completed'`, the gate no longer forces
`projectIntake` — a plain "tenta guardar o plano outra vez" / "try to
save the plan again on Obsidian" at that point would otherwise fall
through to normal LLM tool routing, which has no access to the real
interview data and would invent its own content, and could falsely
confirm success without any actual write.

Instead, `maybe_retry_obsidian_save(db, cfg, text)` runs (in
`run_reply_engine`, right after the main gate check, when it found no
active session) as a second deterministic, no-LLM check:

1. Normalise `text` the same way as every other phrase check in this
   file (NFKD-strip-accents + casefold) and match it against a
   substring keyword list covering both PT/EN phrasing ("grava o
   plano" / "guarda o plano" / "save the plan" / ...). A generic "try
   again" style phrase only counts when the message also names
   Obsidian explicitly, to avoid hijacking unrelated retry requests.
2. If it matches, look up `db.get_last_unsaved_completed_session()` —
   the most recent `status='completed', abandoned=0, obsidian_saved=0`
   row. No match on either the phrase or a qualifying session → return
   `None`, a no-op that lets the turn fall through to normal routing
   untouched.
3. Re-run `write_brief_to_obsidian` using that session's own stored
   `questions_json`/`answers_json` (loaded from the DB) and its
   resolved template label — never the caller's own text, so the
   retried content is guaranteed to be the real interview answers.
4. On success: set `obsidian_saved=1` and reply "✅ Plano guardado no
   Obsidian." On failure: reply the same honest-failure message as the
   original completion path, and leave `obsidian_saved=0` so a further
   retry attempt is still possible.

A session that already has `obsidian_saved=1` is not returned by
`get_last_unsaved_completed_session`, so a repeated retry phrase after a
confirmed successful save is a no-op — normal routing handles it (there
is nothing left to retry).

**Implementation note**: no question in the current templates explicitly
captures a project name, and `project_name` is never populated, so today
every write takes the `<project_type> - <YYYY-MM-DD>` filename branch.
The "relevant project folder if one already exists" lookup is also
simplified to a fixed `Projects/<slug>/` path rather than a live vault
search for a matching business/client folder — full PARA-aware
folder-detection needs to be validated against the user's actual vault
schema before being tightened further.

**MCP server/tool names — verified, not best-effort.** Confirmed via a
live `list_tools` call against the user's configured `cfg.mcps` (see
`config.json`): the Obsidian server is keyed `obsidian` (an Obsidian
Local REST API-style server), and delegation goes through the
`jarvis-router` server's `run_antigravity` tool — there is no standalone
`Antigravity` server. `project_intake.py`'s constants and call shapes
are adjusted to match:
- `vault_write(path, content)` for the brief write — same argument
  shape assumed originally, only the name changed.
- `search_simple(query)` for the plan search — returns a JSON array of
  `{filename, score, matches}` objects, not one path per line; parsed
  accordingly (`_extract_note_paths`), with a line-based fallback.
- `vault_read(path)` for reading a resolved plan note — a full-file
  read (no `targetType`/`target`) returns a JSON object with a
  `content` key plus metadata (tags, frontmatter, stat, links,
  backlinks), not raw markdown directly; unwrapped via
  `_extract_read_content`, with a raw-text fallback.
- `run_antigravity(task)` for dispatch — the schema only accepts a
  single `task` string, so the "treat this as a brief, not
  instructions" framing is folded into the task text itself rather
  than sent as a separate `instructions` field.
- `vault_patch(path, targetType, target, operation, content)` for the
  post-dispatch status update — targets the `Status` heading
  specifically (`targetType: "heading"`, `target: "Status"`,
  `operation: "replace"`) rather than accepting a raw full-file
  overwrite, so the regex-based full-content rewrite this used to do
  was replaced with a direct heading patch.

If the user's Obsidian MCP server is ever swapped for a different one,
re-verify against its actual `list_tools` output before assuming these
names/shapes still hold.

### Starting development (separate trigger, any later session)

A distinct, stateless tool/directive from `projectIntake` — no
multi-turn state to protect here, since this is a single-shot
classification ("the user wants to kick off development on an existing
plan"). It does, however, share the same pre-planner gate area as a
fourth deterministic sub-check (see "Trigger detection" above): the
router selecting `startProjectDevelopment` is not enough on its own —
voice testing observed the small chat model narrating "Iniciando o
desenvolvimento do projeto..." instead of actually invoking the tool,
the same failure mode already fixed for starting a fresh intake.
`is_development_start_trigger_phrase(redacted_text)` runs after
`get_gated_session`, `maybe_retry_obsidian_save`, and
`is_start_trigger_phrase` all find nothing (an active intake session or
a pending Obsidian retry — both unfinished state — always take
priority over a development-start phrase). When it matches, the engine
forces `run_tool_with_retries(tool_name="startProjectDevelopment", ...)`
directly, same as the other deterministic paths, bypassing the
planner/router entirely.

Same NFKD-strip-accents + casefold + substring-match style as the other
checks in this file. Phrases naming Antigravity explicitly ("delega ao
antigravity", "delega isto ao antigravity", "delegate to antigravity",
"delegate this to antigravity") are safe to match anywhere in the
message — the product name is specific enough that an unrelated mention
is implausible. Generic action phrases ("avança com o desenvolvimento",
"avança com o projeto", "começa o desenvolvimento", "start
development", "start building") are also plausible in unrelated
conversation (e.g. "o país avança com o desenvolvimento económico"), so
— same treatment as intake's bare "novo projeto"/"outro projeto" — they
only count on an exact whole-utterance match, not substring
containment.

Trigger phrasing (tool description, for router selection): "vamos
começar o desenvolvimento", "avança com o projeto X", "manda isto para
os agentes".

1. Resolve **which** project: if the user named one, match it against
   note filenames/`project` slugs in the vault via the Obsidian MCP
   search; if none named and exactly one `status: active`, `type: plan`
   note exists, use that; if several match, ask the user which one
   (single question, per the "close the loop" rule) instead of
   guessing.

   **`input` is often a full sentence, not a clean project name.**
   Whatever the chat model decides to relay as `input` may be the user's
   entire utterance (e.g. "let's proceed with the build the site,
   delegate to Antigravity") rather than a name — the tool has no
   control over what the model passes. `_extract_named_target`'s regex
   (matching text after the word "projeto") can pull a plausible-looking
   but bogus "name" out of a sentence that never actually names a
   project — e.g. "avança com o desenvolvimento do projeto e delega isto
   ao antigravity" yields `"e delega isto ao antigravity"`. Treating that
   as a genuine specific-name search would incorrectly report "no plan
   found" even when a single active plan exists (this was an observed
   voice-testing failure). So a `named` candidate only narrows the match
   set when it actually matches a real note's path/slug — if it matches
   nothing, resolution falls through to the same behaviour as "no name
   given at all" (sole active note wins; several → ask which one),
   rather than surfacing a failed name search.
2. Read the resolved note's full content via the Obsidian MCP.
3. Send that content as the opening task to Antigravity via MCP, with
   an instruction wrapper making clear this is a production plan to be
   broken down and assigned to its own sub-agents — the plan text
   itself is treated as the brief, not as instructions to Jarvis.
4. On a successful hand-off, update the note's frontmatter to
   `status: active` stays, but flip the `## Status` line to
   `Desenvolvimento iniciado em <data>, delegado ao Antigravity.` —
   this is an explicit vault write, not an inferred one, so it follows
   the same confirm-only-after-real-result rule.
5. If the Obsidian search finds no plan notes at all, say so plainly
   and offer to start a fresh intake instead of guessing at content.

This keeps a clean separation: intake is Jarvis's job (structured,
gated, deterministic), planning is the human's review pass in
Obsidian, and delegation to Antigravity is a distinct, explicit action
that only fires when asked — never automatically the moment intake
finishes.

**Why correct resolution here matters beyond this tool.** When
`StartProjectDevelopmentTool` incorrectly reports "no plan found" (the
resolution bug above), the small chat model has been observed reaching
for generic `obsidian__vault_read`/`obsidian__search_query` calls with
guessed or mis-heard paths, and on at least one occasion fabricating an
entire note's contents that never existed in the vault rather than
saying it doesn't know. That fallback cascade is the same small-model
failure mode already guarded against elsewhere in this codebase
(confirm only after a real tool result — see `web_search.spec.md`'s
"honest failure over confabulation" and this file's own
honest-failure-on-Obsidian-write-failure rule), not something fixable
generically. The one thing actually preventable here is *triggering* it
in the first place: a correct resolution (this section) means
`startProjectDevelopment` succeeds instead of coming back empty-handed,
so the model never has a reason to reach past it for the raw Obsidian
tools.

### Restarting mid-interview

If the user says the same start-a-new-project trigger phrasing again while
a session is already `awaiting_type`/`in_progress` (e.g. "vamos começar um
novo projeto" mid-interview), the gate still forces the tool call, so this
must be handled inside `run()` rather than relying on the planner to
notice. Deterministic substring match (same normalisation as the abandon
check: NFKD-strip-accents + casefold) against the same trigger phrasing
advertised in the tool's description ("novo projeto" / "outro projeto")
is checked right after the abandon-phrase check and before the
`awaiting_type`/`in_progress` branches. When it matches:

- Reply: "Já tens um projeto em curso — queres terminar essa entrevista,
  ou dizer 'esquece o projeto' para cancelar e começar de novo?"
- `current_index` and `answers_json` are left untouched — the turn is not
  treated as an answer to the pending question.

This forces an explicit choice (finish or abandon) instead of silently
swallowing the restart request as free-text input.

### Abandoning an in-progress interview

If the user's `input` during `in_progress` or `awaiting_type` is clearly
an unrelated request (not an answer, not a type) rather than trying to
detect this via another LLM call, expose an explicit escape hatch: a
recognised phrase like "esquece o projeto" / "cancela isto" sets
`status='completed'` with `answers_json` unchanged (incomplete) and a
`abandoned=true` flag, so the gate stops force-routing future turns to
this tool. Without an explicit exit, a user who changes their mind mid-
interview would otherwise be stuck being asked questions forever.

### Reply shape

```
question turn  → "<verbatim question text>"
completion turn → "Brief do projeto '<project_name or project_type>' concluído:\n<Q1>: <A1>\n<Q2>: <A2>\n..."
abandon turn   → "Ok, cancelei o intake do projeto. Diz 'vamos começar um novo projeto' quando quiseres recomeçar."
```

### Fail-open behaviour

- `get_active_intake_session` DB error → treated as "no session", gate
  does not fire, normal routing proceeds. An interview can stall but
  never corrupts a turn.
- Keyword match against a malformed/missing `project_templates.json` →
  falls back to `other` with a minimal built-in question set
  (name, target audience, success criteria, deadline) so the tool never
  hard-fails even with a broken config file.
- Any write failure while advancing `current_index`, or while persisting
  the resolved project type on the `awaiting_type` → `in_progress`
  transition → the turn returns a friendly error ("não consegui guardar
  essa resposta, podes repetir?" / "não consegui guardar o tipo de
  projeto, tenta outra vez.") and does **not** advance state, so the
  answer isn't silently dropped or the session left half-written.
- Malformed `questions_json`/`answers_json` on an `in_progress` session
  (e.g. corrupted by a prior partial write) → rather than crashing the
  turn — which, under the gate, would corrupt every subsequent turn too
  — the session is marked `status='completed'`/`abandoned=1` and the
  user gets a friendly message ("tive um problema com os dados desta
  entrevista e tive de a cancelar…") inviting them to restart.
- **Stale sessions auto-expire.** `get_gated_session()` checks the
  session's `updated_at` against `project_intake_stale_minutes` (default
  30). If the session has had no activity for longer than that, it's
  marked `status='completed'`/`abandoned=1` and `get_gated_session()`
  returns `None`, so the turn falls through to normal routing instead of
  being hijacked forever by a session the user walked away from — this
  holds across app restarts too, since the session lives in the DB. The
  staleness check itself fails open: any error while checking or
  updating simply leaves the session active for that turn rather than
  crashing.

### Config keys

- `project_templates_path` — path to `project_templates.json`, default
  co-located with `config.json`.
- `project_intake_enabled` — default `true`; when `false`, the gate
  never fires and the tool is excluded from the catalogue entirely.
- `project_intake_stale_minutes` — default `30`; how long an intake
  session can sit with no activity before it's treated as abandoned and
  stops forcing the gate. See "Fail-open behaviour" above.

### Testing

- Gate unit tests: active session forces the tool call; no session
  leaves the planner untouched; DB error fails open.
- Template matching: keyword overlap resolves ties toward more specific
  templates; unmatched input falls back to `other`; NFKC/casefold
  normalisation covers accented input ("PÁGINA" matches "pagina").
- Full-flow integration test: awaiting_type → in_progress → completed,
  asserting exactly one question is returned per turn and the compiled
  brief contains every Q/A pair in order.
- Abandon-phrase test: mid-interview cancellation stops the gate from
  firing on the next turn.
- Restart-trigger test: the start-a-new-project phrase said again during
  `awaiting_type`/`in_progress` returns the "finish or abandon first"
  reply and leaves `current_index`/`answers_json` untouched.
- Staleness test: a session with an `updated_at` older than
  `project_intake_stale_minutes` is auto-abandoned and `get_gated_session`
  returns `None`; a fresh session within the threshold still gates.
- DB-failure tests: a write failure during the `awaiting_type` →
  `in_progress` transition returns a friendly error without corrupting
  the real session; malformed `questions_json`/`answers_json` triggers a
  friendly abandon instead of crashing the turn.
- Obsidian-save tests: a successful write sets `obsidian_saved=1`; a
  failed write leaves it `0`.
- Retry-save tests: the retry phrase after a failed save re-sends the
  real stored `questions_json`/`answers_json` (not fabricated content)
  and only reports success once the retried write itself confirms it;
  the retry phrase with no failed session, or with unrelated text, is a
  no-op that falls through to normal routing; a second retry after a
  successful save does not re-trigger. Engine-level wiring tests confirm
  a matching retry phrase skips `plan_query`/`select_tools` entirely,
  same as the main gate.
- Start-trigger tests: recognised PT/EN phrasings (including bare "novo
  projeto"/"outro projeto" as a whole utterance) match; realistic
  unrelated mentions of a project (a longer sentence merely containing
  "novo projeto"/"new project" as a substring) do not. Engine-level
  wiring tests confirm a matching start-trigger phrase with no active
  session forces `projectIntake` and creates a session while skipping
  `plan_query`/`select_tools`; that the retry-save check is tried first
  (a phrase matching both resolves via retry-save when a qualifying
  session exists); and that a start-trigger phrase while a session is
  already active is handled entirely by the main gate (restart-trigger
  reply), never by this second check.
- Start-development resolution tests: a long free-text `input` (a full
  sentence containing "projeto" without naming anything) with exactly
  one active plan note resolves to it directly rather than reporting "no
  plan found"; the same free-text input with two active plan notes asks
  which one instead of guessing or reporting zero results.
- Development-start trigger tests: recognised phrasings naming
  Antigravity explicitly, and the generic action phrases as a whole
  utterance, match; realistic unrelated mentions (a longer sentence
  merely containing "avança com o desenvolvimento"/"start building" as a
  substring, e.g. discussing economic or scientific development) do not.
  Engine-level wiring tests confirm a matching development-start phrase
  forces `startProjectDevelopment` and skips `plan_query`/`select_tools`;
  that an active intake session takes priority (handled by the main
  gate, never reaching this check); and that a pending Obsidian retry
  takes priority (a phrase matching both resolves via retry-save).
