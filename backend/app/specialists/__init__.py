"""Hosted specialists: OCR, figure description, speech-to-text.

Each adapter talks to one provider through ``reliability.hosted.hosted_call`` (quota governor,
circuit breaker, bounded retries). Results are cached per unit of work (see ``cache``), so a retry
or a re-upload never pays twice for the same page, figure or recording.
"""
