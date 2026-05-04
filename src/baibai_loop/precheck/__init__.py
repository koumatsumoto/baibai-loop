"""Mechanized self-review checks for anti-patterns (AP-06 etc.)."""

from .source_refs import OutlookFinding, scan_outlook_source_refs

__all__ = ["OutlookFinding", "scan_outlook_source_refs"]
