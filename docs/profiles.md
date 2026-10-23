---
layout: default
title: Processing Profiles
---

# Processing Profiles

News needs context. An engineering deep dive needs an explanation of the solution.
A profile tells Horizon what to look for, how to judge it, and what to write—using
Markdown prompts and a JSON block definition.

Profiles define reusable editorial rules. Your sources, AI model, score thresholds,
digest limits, languages, and delivery channels stay in the runtime configuration.
Each item is routed to one profile; a candidate list lets AI choose among several,
rather than process the item with all of them.

## Built-in Profiles

| Profile | What it looks for | What it produces |
| --- | --- | --- |
| `tech-news` | Releases, incidents, research results, and technology-industry developments | Concrete changes and necessary background, with relevant past coverage, impact, and community discussion when useful |
| `tech-blog` | Engineering deep dives, tutorials, investigations, and retrospectives | Background, solution, and takeaways |
| `finance-news` | Markets, macroeconomics, company finance, and economically material policy | Summary and background; direct impact when useful |
| `ai-creator` | AI developments with potential for content creation | Summary; timely hooks, content angles, and community discussion when useful |

Start with a built-in profile, then adjust its prompts or blocks to match your reading needs.

## Directory Layout

Each profile lives under `profiles/<id>/` with the same four-file layout:

```text
profiles/tech-blog/
|-- profile.json
|-- match.md
|-- analysis.md
`-- enrichment.md
```

- `profile.json` defines the profile contract.
- `match.md` tells automatic routing what content belongs to the profile.
- `analysis.md` defines the first-pass analysis and scoring rubric.
- `enrichment.md` defines how to write the localized output blocks.

## Try a Different Reading Style

For an engineering feed, use `tech-blog` to extract the problem, solution, and
lessons. Add an entry like this to `sources.rss`:

```json
{
  "name": "NVIDIA CUDA Technical Blog",
  "url": "https://developer.nvidia.com/blog/tag/cuda/feed/",
  "profile": "tech-blog",
  "content_extractor": "trafilatura"
}
```

`trafilatura` is included in the base install. It fetches the article body;
extraction failures fall back to the feed excerpt. The blog profile uses larger
input budgets and head-middle-tail sampling for long articles.

To create your own profile:

1. Copy a profile directory and give its `profile.json` a unique `id` and name.
2. Edit `match.md` and `analysis.md` to describe the content and evaluation criteria.
3. Set the output blocks in `profile.json` and their writing instructions in `enrichment.md`.
4. Set a source's `profile` to your new ID, then tune its threshold under `processing.profile_settings`.

No Python changes are needed when using the existing block type and tools. Each
new profile also joins the candidates for unrestricted automatic routing.

## Contributing a Profile

A profile is a reusable editorial policy for a content domain, not a user
account, source list, or collection of personal interests. It tells Horizon:

1. which items belong to the domain (`match.md`),
2. how to evaluate and score them (`analysis.md`), and
3. which content blocks to produce and how to write them (`profile.json` and
   `enrichment.md`).

To contribute a built-in profile, add `profiles/<id>/` with those four files and
open a focused pull request. No Python changes are normally required.

Before submitting, check that the profile:

- covers a clear content domain that is useful to more than one source list or
  individual user,
- has a meaningful routing, evaluation, or output difference from existing
  profiles,
- states both what belongs and what does not belong in `match.md`,
- defines a concrete 0-10 rubric in `analysis.md`,
- keeps generated blocks specific, non-overlapping, and grounded in supplied
  content or declared tools,
- contains no credentials, private sources, user-specific thresholds, or the
  local console's `.user-profile` marker, and
- passes `uv run pytest tests/test_profiles.py tests/test_prompting.py -q`.

Built-in profiles are loaded as automatic-routing candidates, so maintainers may
ask contributors to narrow ambiguous matching rules or clarify overlap before a
profile is merged.

Configure discovery in `data/config.json`:

```json
{
  "processing": {
    "profiles_dir": "profiles",
    "default_profile": "tech-news",
    "profile_settings": {
      "tech-news": {
        "threshold": 7.0,
        "topic_dedup": true
      },
      "tech-blog": {
        "threshold": 4.0,
        "topic_dedup": false
      }
    }
  }
}
```

`default_profile` must name a loaded profile. Horizon fails to start if no
profiles are found or the default does not exist.

## Profile Schema

```json
{
  "id": "tech-news",
  "name": "Technology News",
  "display_names": {
    "zh": "科技新闻"
  },
  "match": "match.md",
  "analysis": "analysis.md",
  "content": {
    "analysis_max_chars": 1000,
    "enrichment_max_chars": 8000,
    "sampling": "prefix"
  },
  "enrichment": {
    "prompt": "enrichment.md",
    "blocks": [
      {
        "id": "summary",
        "type": "section",
        "tools": [],
        "primary": true
      },
      {
        "id": "background",
        "type": "section",
        "tools": ["history_search", "web_search"]
      },
      {
        "id": "community_discussion",
        "type": "section",
        "tools": [],
        "optional": true
      }
    ]
  }
}
```

| Field | Description |
| --- | --- |
| `id` | Unique profile ID. It starts with a lowercase letter and may contain lowercase letters, digits, `_`, and `-`. |
| `name` | Human-readable name used in the matching catalog. |
| `display_names` | Optional language-keyed names used as digest section headings. |
| `match` | Profile-relative path to the matching prompt. |
| `analysis` | Profile-relative path to the analysis prompt. |
| `content` | Input budgets and long-content sampling strategy for AI stages. |
| `enrichment.prompt` | Profile-relative path to the enrichment prompt. |
| `enrichment.blocks` | Contract for localized output blocks. At least one block is required. |

Block IDs use the same format as profile IDs and must be unique within a
profile. The only supported block `type` is `"section"`. Blocks are required by
default; set `optional` to `true` when they may be omitted.

| Block field | Description |
| --- | --- |
| `id` | Unique block ID within the profile. |
| `type` | Must be `"section"`. |
| `tools` | Tools allowed for this block. Declare it on every block; use `[]` when none are allowed. |
| `optional` | Whether output may omit the block. Defaults to `false`. |
| `primary` | Render this required block directly below the item title without a block heading. At most one block may be primary. Defaults to `false`. |

Prompt paths cannot escape their profile directory. Unknown fields are rejected
in profile JSON.

## Source Routing

Set `profile` on a source entry to route its items directly:

```json
{
  "sources": {
    "rss": [
      {
        "name": "Example",
        "url": "https://example.com/feed.xml",
        "profile": "tech-news"
      }
    ]
  }
}
```

Set `profile` to an array to restrict automatic matching to a candidate subset:

```json
{
  "channel": "zaihuapd",
  "profile": ["tech-news", "finance-news"]
}
```

Routing follows these rules:

1. An explicit profile ID uses that profile and skips AI matching.
2. A missing `profile` or `"auto"` invokes AI matching against every loaded
   profile's `match.md`.
3. A non-empty profile array invokes AI matching only against those candidates.
4. Unknown, duplicate, blank, or `"auto"` entries in a candidate array are errors.
5. If candidate matching fails, Horizon uses `processing.default_profile` when
   it is a candidate, otherwise the first candidate. Unrestricted matching falls
   back to `processing.default_profile`.

All source types support profile routing. For sources with nested entries, put
`profile` on the item-producing configuration, such as a GitHub entry, RSS feed,
Reddit subreddit or user, or OpenBB watchlist. Top-level single configurations,
such as Hacker News, Twitter, OSS Insight, GDELT, and Google News, carry the
field directly.

## Local Console

An optional browser console manages profiles and profile routing on the machine
that holds your configuration. Start it from the directory where you run
`horizon`, with the same data and config options:

```bash
uv run python -m src.web.server [--port 8765] [-d PATH] [-c PATH]
```

Open the printed `http://127.0.0.1:8765/` address. The console has no login, so
`--host` accepts only loopback addresses such as `127.0.0.1`, `::1`, or
`localhost`. It answers only requests whose `Host` header names a loopback
address on the console's port, requires a
same-origin request and a per-start CSRF token for every change, and loads no
external assets. Do not expose it through a proxy or port forwarding.
Configuration responses include only the default profile, profile settings, and
each source's name, enabled state, and route; they never include source URLs,
credentials, or other configuration values.

### Profile Lifecycle

- **Create** a profile from a new ID, or **copy** any loaded profile to a new ID.
  The console writes the four-file layout plus a `.user-profile` marker file
  to `profiles/<id>/`.
- **Edit** and **delete** work only on profiles with that marker. Built-in
  profiles and directories added by hand are read-only in the console; copy
  them to make changes.
- Profile IDs cannot be changed. They start with a lowercase letter and use at
  most 64 lowercase letters, digits, `_`, or `-`; `auto` and Windows device
  names are reserved. Prompt file names are fixed to `match.md`, `analysis.md`,
  and `enrichment.md`.

When profiles fall back to the packaged built-in resources (an installed
package without a `profiles/` directory), or the profile directory is not
writable, the console is read-only. Set `processing.profiles_dir` to a writable
directory to manage profiles.

### Validation and Deletion

Every save validates `profile.json` against the schema above, rejects unknown
tools and empty prompts, and then loads a fresh registry from a copy of the
whole profile directory with the change applied. Files are written only after
that check passes, so a rejected save leaves the existing profile unchanged. A
new profile appears through a single directory rename.

An edit prepares a complete replacement directory in a hidden `.console-*` work
directory first: the new `profile.json` and prompts keep the permission bits of
the files they replace, and the marker and any other files in the profile
directory are copied with their contents and permission bits (not their owner
or extended attributes). A failure while preparing leaves the profile
untouched. If anything in the profile directory changed while it was being
copied, the save is rejected. The console then renames the profile directory to
a hidden backup and renames the replacement into its place. If the second
rename fails, the backup is renamed back; if that also fails, the error names
the backup directory that still holds the previous files, so do not delete that
backup until you have restored it. Between the two renames, which normally take
microseconds, the profile directory does not exist, so a pipeline run starting
at that instant may not find the profile. A program that keeps a profile file
open and writes to it after the swap writes to the replaced copy, and that
change is lost. Work directories are never loaded as profiles; ones left behind
by a failed cleanup after a successful save can be deleted.

Delete is rejected for `processing.default_profile` and for any profile still
named by `processing.profile_settings`, `digest.profile_order`, profile-based
`digest.category_groups`, or a source route. The error lists each reference so
you can remove it first.

Profiles and the config file are saved against the revision you loaded. The
console compares the files with that revision again immediately before writing
and rejects the save if they changed, for example in another tab or editor.
For profile files, console processes that share a profile directory take turns
through a `.console-lock` file in that directory. Opening a profile reads it
under the same lock, and its revision is computed from exactly the content
shown, so a save based on content that is no longer on disk is rejected. The config file has no such
lock, and editors and other tools do not use it. A change made in the instant
between the final comparison and the write is overwritten without warning.

### Routing

The Routing page changes only `processing.default_profile`,
`processing.profile_settings` (`threshold` and `topic_dedup`), and the `profile`
field of sources present in the config file. Each source can use:

- **Automatic**: removes `profile`, so AI matches against every loaded profile.
- **Fixed profile**: sets `profile` to one ID and skips AI matching.
- **Candidates**: sets `profile` to an array and restricts AI matching to those
  profiles.

Other settings and `${VAR}` references are written back unchanged; the file is
reformatted as two-space JSON. Saves pass the same profile reference checks that
`horizon` performs at startup. To route a source that currently relies on
defaults, such as Hacker News, add its section to the config file first.

### Changes Without Restart

`horizon` reads the config file and profile directory at the start of every run,
so the next run uses saved changes without restarting a service or container.
The Run page starts the pipeline with the console's data and config paths,
shows the latest 300 log lines, and lists recent summaries. The log masks
values of at least six characters from environment variables with
credential-like names, from variables the config file references through
`${VAR}` or `*_env` settings, and from plain strings under credential-like keys.
It also masks URL user information, credential-like query parameters, and common
authentication headers. Other output appears as the pipeline prints it.

Saves are rejected only while a run started by the same console process is in
progress. The console does not detect runs started any other way, such as the
`horizon` command, cron jobs, or containers, so avoid saving while those run.

With Docker Compose, the image contains its own copy of `profiles/`. To manage
profiles for container runs, copy `profiles/` to `data/profiles/`, set
`processing.profiles_dir` to `"data/profiles"`, and start the console on the
host from the project directory. The next `docker compose run` reads the
mounted files.

## Analysis

After routing, Horizon sends the item to the selected profile's `analysis.md`
prompt. A successful analysis contains a 0-10 score, a reason, a one-sentence
summary, and tags. A failed analysis may be stored with a null score. The profile
owns the rubric, so profiles can evaluate different content forms by different
standards.

## Filtering

Filtering is a user preference configured by profile ID under
`processing.profile_settings`:

```json
{
  "processing": {
    "profile_settings": {
      "tech-news": {
        "threshold": 8.0
      }
    }
  }
}
```

`threshold` must be between 0 and 10. Horizon keeps items whose analysis score
is greater than or equal to that threshold. Set it to `null` or omit settings for
a profile to bypass score filtering. An MCP threshold supplied for a single
operation takes precedence over these configured values.

The top-level `collection` configuration controls `time_window_hours`. Optional
balanced digest limits such as `category_groups` and `max_items` belong to the
top-level `digest` configuration and run after profile filtering and topic
deduplication.

## Enrichment Blocks And Tools

`enrichment.blocks` defines the exact block IDs available to output.
Required blocks must be present; optional blocks can be omitted when they add no
useful content. Generated output cannot contain unknown or duplicate blocks.

Tools are allowed per block through its `tools` array. The built-in tools are
`web_search` (external web results) and `history_search` (past Horizon digests).
A block may use a tool only when it explicitly declares it. Use an empty array
for blocks that need no tools. Unknown tools are rejected when the enricher is
initialized.

Tool planning receives each block's required or optional status. For required
blocks with tools, the prompt asks the model to use a tool unless the source
already provides enough evidence. A search is not guaranteed; tool failures do
not make a required block optional.

Search-backed statements cite tool results through source references. Horizon
rejects references that were not returned by a tool call.

### Historical news search

`history_search` reads the existing `horizon-YYYY-MM-DD*.md` files under your
data directory's `summaries/` folder. It uses local BM25 ranking over individual
news titles, main summaries, and tags, with extra weight for titles. The index is
loaded lazily once per enrichment batch; it needs no additional dependency,
database, embedding service, or AI relevance-scoring call.

Tool arguments:

```json
{"query": "vLLM batch inference", "days": 90}
```

- `query` is required. Prefer specific product, project, or event names; include
  useful aliases rather than broad terms such as "AI news".
- `days` is optional, defaults to 90, and accepts 1–365. Only digests before the
  current item's UTC publication date (and before today) are eligible.
- Dates come from filenames and identify the **digest**, not necessarily the
  original event date.
- Each item gets at most one history search, shared across output languages.
  It returns at most three candidates, each with a dated title, original URL,
  archive filename, and up to 500 characters of the main summary.
- Results exclude the current URL and collapse repeated links, including
  translations and fragment/trailing-slash variants. Earlier background,
  historical callbacks, comments, and reference lists are not indexed as evidence.
- Tokenization uses Latin words and overlapping two-character Chinese tokens.
  It can match Chinese phrases without an extra segmenter, but does not infer
  synonyms or cross-language equivalence. No matching terms means no results.

The built-in `tech-news` profile uses history in its existing `background` block.
Its prompt requires a direct predecessor, follow-up, or concrete change, limits
historical references to two, and discards candidates connected only by a broad
topic or company name. Results are candidates, not proof of a relationship.
Normal block generation decides whether to use them, so their short excerpts
add some input tokens but no separate AI screening stage.

To try it, keep past Markdown digests in the same data directory used by the
next run. CLI `--data-dir` and the existing Docker data mount are respected. MCP
uses `summaries/` beside its config file; per-run MCP artifacts alone are not
searched (export a digest with `save_to_horizon_data` to include it). An empty or
missing archive simply returns no results. Scheduled runners need to restore
past digest files before running; publishing old Pages posts alone does not
make them available in a fresh checkout.

## Content Selection

Profiles can control how much source content each AI stage receives:

```json
{
  "content": {
    "analysis_max_chars": 16000,
    "enrichment_max_chars": 24000,
    "sampling": "head-middle-tail"
  }
}
```

`sampling` accepts `"prefix"` or `"head-middle-tail"`. Prefix sampling preserves
the compact behavior used by news profiles. Head-middle-tail sampling keeps the
opening, a middle excerpt, and the conclusion of long-form content. Both limits
must be between 500 and 100000 characters.

## Topic Deduplication

AI topic deduplication is also a runtime preference. Disable it for profiles
where different treatments of the same subject should remain separate:

```json
{
  "processing": {
    "profile_settings": {
      "tech-blog": {
        "topic_dedup": false
      }
    }
  }
}
```

`topic_dedup` defaults to `true` when it or the profile's settings are omitted.

Topic deduplication runs within each resolved profile. Disabling it does not
disable the orchestrator's earlier URL deduplication: items with the same
normalized URL and requested profile or candidate list are still merged before
analysis. Different requested routes stay separate at that stage; individual
scrapers may also deduplicate their results before routing.

## Localized Output

For each language in `ai.languages`, enrichment produces a localized artifact
with:

- a title;
- the profile's required and applicable optional section blocks; and
- cited external sources referenced by those blocks.

Artifacts generated for `zh` are normalized to Simplified Chinese before they
are stored and rendered, including older artifacts read during rendering.

The Markdown briefing renders a block marked `primary` directly below the item
title and before the source line, without a redundant block heading. Profiles
without a primary block show the source first and then render every block under
its bold localized title on the same line as its content. External references
follow the blocks when used. Items
are grouped by Profile: the briefing title is H1, localized Profile names are H2
sections, and items are H3 headings. Set `digest.profile_order` to control the H2
section priority. When this list is non-empty, loaded Profiles omitted from it
are appended in discovery order; unknown or duplicate IDs are rejected. With an
empty or omitted list, sections follow the order in which profiles first appear
in the selected items. The final Markdown is rendered by code, without another
AI summarization call.
