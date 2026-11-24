# User-facing output language

Use the user's requested language or, by default, the language of the current conversation for chat summaries, check findings, and report titles, text, and diagram labels.

Pass the same language as a BCP 47 tag to `lw.py view --lang`, for example `en`, `zh-Hans`, or `fr-CA`. Built-in HTML labels support English and Simplified Chinese. For other languages the HTML keeps the authored content in the chosen language and uses English interface labels.

Keep code, Git diffs, paths, identifiers, commands, logs, and copied record entries as supplied. Explain them in the chosen language next to the evidence.
