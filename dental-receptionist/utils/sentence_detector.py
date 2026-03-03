import re


# Minimum characters before we consider splitting on a sentence boundary.
# Prevents splitting very short openers like "Hi." into a lone word.
MIN_SENTENCE_LENGTH = 25


def extract_sentence(text: str) -> tuple[str, str]:
    """
    Find the first complete sentence (or long clause) in `text`.

    Returns (sentence, remainder).
    If no boundary is found yet, returns ("", text).

    Splitting rules (in priority order):
      1. Text ending with .  !  ?  followed by a space
      2. Text longer than 60 chars ending with ,  ;  followed by a space
         (catches long spoken clauses so ElevenLabs starts early)
    """
    if len(text) < MIN_SENTENCE_LENGTH:
        return "", text

    # Rule 1: hard sentence boundary
    match = re.search(r'([.!?])\s+', text)
    if match:
        end = match.end()
        return text[:end].strip(), text[end:]

    # Rule 2: soft clause boundary (only for longer buffers)
    if len(text) >= 60:
        match = re.search(r'([,;])\s+', text)
        if match:
            end = match.end()
            return text[:end].strip(), text[end:]

    return "", text


def flush(text: str) -> str:
    """Return whatever is left in the buffer (end of Claude's response)."""
    return text.strip()
