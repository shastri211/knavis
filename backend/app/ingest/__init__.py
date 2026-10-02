"""Ingestion: file -> structured elements -> chunks.

Cheapest path first. Everything in this package runs locally and deterministically; the
paid or rate-limited specialists (OCR, vision, speech-to-text) are called from ``app.jobs``
only for the pages or files that need them, and their output is cached by content hash.
"""
