####################################################################################################
# Project name      : Outlook Support Classification Agent                                         #
# Business owner    : <fill: business owner / team>                                                #
# Notebook Author   : <fill: author name / team>                                                   #
# Date              : <fill: date>                                                                 #
#                                                                                                  #
# Purpose of file:                                                                                 #
# In-process knowledge-base SOURCE supplying the candidate articles the agent selects from.        #
#   1. Load a local kb_index.json whose records mirror the ServiceNow search-result shape.         #
#   2. Return ALL loaded records as candidates for the Foundry agent to choose from (no scoring).  #
#   3. Expose is_known_kb_id so the turn orchestrator can reject fabricated article numbers.       #
#                                                                                                  #
# Source:-                                                                                         #
#   - Standard library json parses the local index file into Python dicts.                         #
#   - typing (Any / Optional) supplies the type hints used on the public API surface.              #
#   - telemetry_logging (LogFactory / StructuredLogger) provides the structured JSON logger;       #
#       every load / candidate / skip event is logged with a stable schema + correlation id.       #
####################################################################################################

# ============================================ Imports =============================================
from __future__ import annotations  # Enable postponed evaluation of annotations (PEP 563) for forward hints  # future import

import json  # Parse the local kb_index.json file into Python objects                               # stdlib json
from typing import Any, Optional  # Type hints for arbitrary record values and optional arguments   # stdlib typing

from .telemetry_logging import LogFactory, StructuredLogger  # Structured logger factory + logger type  # logging


# =========================================== Exceptions ==========================================
class KnowledgeBaseSourceError(Exception):  # Domain error raised when the local index cannot load
    """Raised when the local knowledge-base index is missing, unreadable or malformed.

    What this class is:
        - The single domain error for the knowledge-base source: it signals that the local index
          could not be loaded (missing file, invalid JSON, or a non-list "searchResults") in one
          typed error.

    Why this exists:
        - To give the turn orchestrator one specific exception to catch, and to keep the message
          generic so filesystem paths / parser internals are never leaked to the caller.

    Security and production notes:
        1. The message is intentionally generic - it never embeds the file path, raw file bytes or
           the underlying parser exception text, so no internals reach an end user.
        2. Every failure to obtain the knowledge base surfaces as this one error, so the caller
           contract stays the same whatever the source is.

    Example:
        >>> raise KnowledgeBaseSourceError("The knowledge-base source could not be loaded.")  # doctest: +SKIP
    """


# ======================================== Knowledge-base source ==================================
class KnowledgeBaseSource:  # Loads the local ServiceNow-shaped index and returns all records as candidates
    """In-process knowledge-base source supplying the candidate articles for one search.

    What this class is:
        - The knowledge-base source: it loads a local kb_index.json whose records mirror the
          ServiceNow search-result shape and returns them as the candidate pool. Selection is left
          entirely to the Foundry agent.

    Why this exists:
        - To hold the knowledge base behind one stable seam. The candidate contract is a list of
          ServiceNow-shaped dicts, so a source that returns the same record shape slots in here
          without any downstream code change.

    Security and production notes:
        1. Article numbers and content come from the trusted local index (returned verbatim); no
           model touches them here, so nothing is invented or paraphrased by this module.
        2. Grounding (is_known_kb_id) lets the turn orchestrator reject any article id the agent
           fabricates by validating it against the numbers actually present in the index.
        3. The whole candidate pool is handed to the agent on every search, so the index size
           directly drives per-turn input tokens and latency. Keep the pool scoped, or filter
           behind this seam.

    Example:
        >>> source = KnowledgeBaseSource("kb_index.json", log_factory)  # doctest: +SKIP
        >>> source.is_known_kb_id("KB0024755")  # doctest: +SKIP
        True
    """

    def __init__(self, index_path: str, log_factory: LogFactory) -> None:  # Load the index + build the number set
        """Read and parse the local index, then build the known-number set.

        What this method does:
            - Reads + JSON-parses the file at index_path, takes the list under "searchResults"
              (default [] when absent), and stores it as self._records. It then builds
              self._known_numbers from each record's "number" column for O(1) grounding.

        Why it exists:
            - To load the knowledge-base source once per worker so every turn reuses the parsed
              records and the pre-built number set instead of re-reading the file.

        Security and production notes:
            1. Records with no resolvable "number" are skipped (a WARNING logs the skipped count) so
               they can never be handed out as an ungroundable candidate id.
            2. A missing file, invalid JSON or a non-list "searchResults" raises the generic domain
               error (after an ERROR log) - no path or parser internals reach the caller.

        Args:
            index_path: Filesystem path to the local ServiceNow-shaped kb_index.json file.
            log_factory: Factory used to obtain the structured logger for this component.

        Returns:
            None.

        Raises:
            KnowledgeBaseSourceError: If the file is missing, not valid JSON, or "searchResults"
                is present but is not a list.

        Example:
            >>> KnowledgeBaseSource("kb_index.json", log_factory)  # doctest: +SKIP
        """
        self._index_path = index_path  # Store the index path for the read below (never surfaced)    # index path
        self._logger: StructuredLogger = log_factory.get_logger("servicenow_kb_source")  # Named structured logger  # logger

        # --- Read + parse the local index file; any failure becomes the generic domain error ---
        try:  # Attempt to read and JSON-parse the local index file                                  # load try
            with open(index_path, "r", encoding="utf-8") as index_file:  # Open the index for UTF-8 reading  # open file
                index_data = json.load(index_file)  # Parse the file contents into a Python object   # parse json
        except (OSError, ValueError) as load_error:  # File-not-found / unreadable / invalid-JSON    # load failed
            self._logger.log(  # Log the load failure at ERROR with the exception class only         # log error
                event="kb_source_load_failed",  # Event name for a load failure                      # event
                correlation_id="startup",  # No per-turn id at construction time                     # correlation id
                level="ERROR",  # Log at ERROR severity                                              # level
                error_type=type(load_error).__name__,  # Record the exception class name only        # error type
            )
            raise KnowledgeBaseSourceError("The knowledge-base source could not be loaded.") from load_error  # Generic domain error  # raise domain

        # --- Extract the searchResults list (default []); a present-but-non-list value is invalid ---
        raw_records = index_data.get("searchResults", []) if isinstance(index_data, dict) else None  # Take the list or flag bad shape  # take list
        if not isinstance(raw_records, list):  # "searchResults" must be a list (or absent -> [])     # shape check
            self._logger.log(  # Log the invalid-shape failure at ERROR                               # log error
                event="kb_source_invalid_shape",  # Event name for a bad top-level shape             # event
                correlation_id="startup",  # No per-turn id at construction time                     # correlation id
                level="ERROR",  # Log at ERROR severity                                              # level
            )
            raise KnowledgeBaseSourceError("The knowledge-base source is malformed.")  # Generic domain error  # raise domain

        self._records: list[dict] = raw_records  # Store all parsed records as the candidate pool     # store records

        # --- Build the known-number set; skip (and count) records with no resolvable number ---
        self._known_numbers: set[str] = set()  # Accumulator for the uppercased, stripped article numbers  # number set
        skipped_count = 0  # Count records skipped because they lack a resolvable "number"            # skip counter
        for record in self._records:  # Iterate over every loaded record                             # each record
            number_value = self._number_of(record)  # Pull this record's "number" column value        # get number
            if not number_value or not number_value.strip():  # Skip records with no usable number    # missing number
                skipped_count += 1  # Increment the skipped-record counter                           # bump skip
                continue  # Move on without adding to the known-number set                           # skip record
            self._known_numbers.add(number_value.strip().upper())  # Store the canonical (upper) number  # add number

        if skipped_count:  # Only log the skip WARNING when at least one record was skipped           # any skipped?
            self._logger.log(  # Warn that some records lacked a resolvable "number"                  # log warning
                event="kb_source_records_skipped",  # Event name for the skipped-record case         # event
                correlation_id="startup",  # No per-turn id at construction time                     # correlation id
                level="WARNING",  # Log at WARNING severity                                          # level
                skipped_count=skipped_count,  # Record how many records were skipped                 # skipped count
            )

        self._logger.log(  # Log a one-time INFO that the source finished loading                    # log info
            event="kb_source_loaded",  # Event name for a completed load                             # event
            correlation_id="startup",  # No per-turn id at construction time                         # correlation id
            record_count=len(self._records),  # Record how many candidates are available             # record count
        )

    # ============================================ Public API =====================================
    def get_all_candidates(self, correlation_id: str) -> list[dict[str, Any]]:  # Return ALL loaded records
        """Return every loaded record as a candidate for the agent to choose from.

        What this method does:
            - Returns self._records unchanged: the full candidate pool the Foundry agent selects
              from. Filtering, ranking and scoring are all left to the agent.

        Why it exists:
            - To model the search seam: the agent is handed the search-result set and makes its own
              selection from it.

        Security and production notes:
            1. Records are returned verbatim from the trusted local index - no model touches them,
               so nothing is invented or reworded on the way out.

        Args:
            correlation_id: The end-to-end correlation id for this turn.

        Returns:
            The list of all loaded records (each a ServiceNow-shaped dict); possibly empty.

        Example:
            >>> source.get_all_candidates("cid")  # doctest: +SKIP
            [{'sysId': '...', 'title': '...', 'columns': [...]}, ...]
        """
        self._logger.log(  # Log that the candidate pool was handed back                             # log info
            event="kb_candidates_returned",  # Event name for a candidate hand-off                   # event
            correlation_id=correlation_id,  # Propagate the per-turn correlation id                  # correlation id
            candidate_count=len(self._records),  # Record how many candidates were returned          # candidate count
        )
        return self._records  # Return every loaded record as the candidate pool                     # return all

    def is_known_kb_id(self, kb_id: Optional[str]) -> bool:  # Report whether an article number is a REAL index entry
        """Return True only when kb_id is a real article number in the loaded index.

        What this method does:
            - Normalises kb_id (strip + uppercase) and reports whether it is present in the set of
              numbers built from the trusted index.

        Why it exists:
            - It is the turn orchestrator's anti-hallucination guard: a number the agent recalls
              that is really in the index is a valid resolution, while one absent from the index is
              a fabrication to reject.

        Security and production notes:
            1. Grounding is strict-equality against the index-derived number set - it can never be
               satisfied by an id the agent invented.
            2. Matching is case-insensitive and whitespace-trimmed so a valid number is not rejected
               over trivial formatting differences.

        Args:
            kb_id: The article number the agent tried to resolve with (may be None or blank).

        Returns:
            True if kb_id is truthy and its stripped, uppercased form is a known number; else False.

        Example:
            >>> source.is_known_kb_id("kb0024755")  # doctest: +SKIP
            True
        """
        return bool(kb_id) and kb_id.strip().upper() in self._known_numbers  # Real only when present in the number set  # grounded?

    # ========================================= Internal helpers ==================================
    @staticmethod  # Declare a static helper (needs neither instance nor class state)
    def _number_of(record: dict) -> Optional[str]:  # Pull the "number" column value from one record
        """Extract the article number from a single ServiceNow-shaped record.

        What this method does:
            - Scans record["columns"] for the entry whose fieldName is "number" and returns its
              "value" (falling back to "displayValue"); returns None when neither is present.

        Why it exists:
            - The article number lives inside the columns list (mirroring the ServiceNow shape), so
              both the load-time number set and any caller need one shared way to read it.

        Security and production notes:
            1. Reads only from the trusted local record - it neither invents nor rewrites a number.
            2. Returns None (rather than raising) on a missing/blank column so the caller decides
               how to handle a record with no usable number.

        Args:
            record: One ServiceNow-shaped record (expects a "columns" list of field dicts).

        Returns:
            The "number" column's value (or displayValue) as a string, or None when absent.

        Example:
            >>> KnowledgeBaseSource._number_of({"columns": [{"fieldName": "number", "value": "KB0024755"}]})
            'KB0024755'
        """
        for column in record.get("columns", []):  # Iterate over the record's column dicts (default [])  # each column
            if isinstance(column, dict) and column.get("fieldName") == "number":  # Match the "number" field  # match field
                return column.get("value") or column.get("displayValue")  # Prefer value, fall back to displayValue  # read number
        return None  # No "number" column was found in this record                                   # not found
