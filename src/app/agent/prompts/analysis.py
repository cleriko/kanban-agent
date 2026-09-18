from __future__ import annotations

# The schema is handed to the model as a generation constraint, not just as advice.
# Ollama enforces it with its `format` parameter; other providers get it in the prompt.
ANALYSIS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "keyPoints": {"type": "array", "items": {"type": "string"}},
        "decisions": {"type": "array", "items": {"type": "string"}},
        "actionItems": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "assignee": {"type": ["string", "null"]},
                    "dueDate": {"type": ["string", "null"]},
                    "confidence": {"type": "number"},
                },
                "required": ["title", "confidence"],
            },
        },
        "questions": {"type": "array", "items": {"type": "string"}},
        "topics": {"type": "array", "items": {"type": "string"}},
        "participants": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "keyPoints", "decisions", "actionItems", "questions"],
}

ANALYSIS_SYSTEM = """\
You extract structure from meeting transcripts. You are precise and you do not invent.

Rules:
- Use only what the transcript actually says. If something was not discussed, leave the
  field empty rather than guessing.
- An action item is something a person committed to doing. "We should think about X" is
  not an action item; "I'll send the draft by Friday" is.
- Write action item titles as short imperative phrases: "Finish onboarding screens".
- Only set assignee when the transcript makes it clear who owns it.
- Only set dueDate when a date or day was actually stated. Use ISO-8601 (YYYY-MM-DD) or
  a plain word like "friday". Otherwise null.
- confidence is 0..1 and reflects how clearly the commitment was made.
- A decision is a conclusion the group reached, not a topic they touched.
- Transcripts come from automatic speech recognition and contain errors. Correct obvious
  mis-hearings silently; do not comment on transcription quality.
- Write in plain prose. No markdown, no bullets inside string values.
"""

ANALYSIS_PROMPT = """\
Meeting: {title}
Date: {date}
Duration: {duration}

Transcript:
---
{transcript}
---

Produce the structured analysis. The summary is two or three sentences covering what the
meeting was actually about and what came out of it.
"""

# Used when a transcript is too long for one context window.
CHUNK_SYSTEM = """\
You are condensing one part of a longer meeting transcript so it can be analysed as a
whole. Preserve anything that looks like a decision, a commitment, an owner, a date or an
open question, including the speaker's name. Drop small talk and repetition. Write plain
prose, no markdown.
"""

CHUNK_PROMPT = """\
Part {index} of {total} of the meeting "{title}".

---
{chunk}
---

Condense this part, keeping every decision, commitment, owner, date and open question.
"""
