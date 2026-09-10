####################################################################################################
# Project name      : Outlook Support Classification Agent                                         #
# Business owner    : <fill: business owner / team>                                                #
# Notebook Author   : <fill: author name / team>                                                   #
# Date              : <fill: date>                                                                 #
#                                                                                                  #
# Purpose of file:                                                                                 #
# The knowledge-base SOURCE: run one search and shape the result into candidates for the agent.    #
#   1. Ask the search endpoint for the articles matching the agent's search description.           #
#   2. Reduce each result to the four fields the agent needs to choose between articles.           #
#   3. Clean the text of search markup and escaped characters.                                     #
#   4. Cap how many candidates one search hands back.                                              #
#                                                                                                  #
# Why the result is reduced:                                                                       #
#   A search result carries record identifiers, table names, duplicated display values, per-field  #
#   labels and a highlighted snippet of the same article body. None of that helps the agent pick   #
#   an article, and every character of it is charged as input tokens on the call that follows.     #
#   A search can return thirty results and one turn can run several searches, so reducing each     #
#   record is what keeps a turn affordable and inside its time budget.                             #
#                                                                                                  #
# Source:-                                                                                         #
#   - Standard library html/re supply entity unescaping and the search-markup stripper.            #
#   - typing (Any / Optional) supplies the type hints used on the public API surface.              #
#   - servicenow_search_client supplies ServiceNowSearchClient + ServiceNowSearchError.            #
#   - app.event_hub (LogFactory / StructuredLogger) provides the structured JSON logger.           #
####################################################################################################

# ============================================ Imports =============================================
from __future__ import annotations  # Enable postponed evaluation of annotations (PEP 563) for forward hints  # future import

import html  # Turn escaped entities such as &quot; back into their characters                      # stdlib html
import re  # Strip the search-markup tags and collapse runs of whitespace                           # stdlib re
from typing import Any, Optional  # Type hints for record values and optional arguments             # stdlib typing

from .servicenow_search_client import ServiceNowSearchClient, ServiceNowSearchError  # Search client + its error  # search client
from app.event_hub import LogFactory, StructuredLogger  # Structured logger factory + logger type   # logging

# The search service wraps matched words in markup so a user interface can highlight them. It is
# noise to a model, so it is taken out before the text is handed over.
_SEARCH_MARKUP_PATTERN = re.compile(r"</?highlight>", re.IGNORECASE)  # Opening/closing markup tags  # markup regex

# Runs of whitespace and line breaks are collapsed to single spaces so the text stays compact.
_WHITESPACE_RUN_PATTERN = re.compile(r"\s+")  # Any run of whitespace                                # whitespace regex

# The result columns worth reading, mapped to the name each becomes in a candidate.
_TITLE_FIELD = "short_description"  # The article's title line                                       # title field
_NUMBER_FIELD = "number"  # The article number the agent resolves with                               # number field
_CONTENT_FIELD = "text"  # The article body                                                          # content field
_CATEGORY_FIELD = "kb_category"  # The article's category                                            # category field


# =========================================== Exceptions ==========================================
class KnowledgeBaseSourceError(Exception):
    """Raised when the knowledge base could not be searched.

    What this class is:
        - The single domain error this module raises, so the turn orchestrator catches one type
          however the search failed.

    Why this exists:
        - To keep the message generic, so no endpoint URL, credential or upstream error body can
          travel back to the caller and reach an end user.

    Example:
        >>> raise KnowledgeBaseSourceError("The knowledge base could not be searched.")  # doctest: +SKIP
    """


# ======================================== Knowledge-base source ==================================
class KnowledgeBaseSource:
    """Search the knowledge base and hand the agent a clean set of candidate articles.

    What this class is:
        - The seam between the turn orchestrator and the knowledge base. It runs one search per
          request and reduces each result to the fields an agent needs to choose an article:
          its number, its title, its category and its body.

    Why this exists:
        - To keep the shape of the search service out of the turn loop. The orchestrator asks for
          candidates and receives a list of small, uniform dicts, whatever the service returns.

    Security and production notes:
        1. Article text comes from the search service and is passed through verbatim apart from
           markup stripping and length capping. No model rewrites it here, so nothing is invented.
        2. Article text is untrusted content: it is placed in the agent's input, so the prompt must
           treat the candidate block as data rather than as instructions.
        3. A failed search is raised, not returned as an empty list. An empty list is
           indistinguishable from "nothing matched" and would end conversations as though the
           knowledge base had been read successfully.

    Example:
        >>> source.search("outlook is not working", "cid")  # doctest: +SKIP
        [{'kb_id': 'KB0024755', 'title': '...', 'category': '...', 'content': '...'}]
    """

    def __init__(  # Keep the search client and the shaping limit
        self,
        search_client: ServiceNowSearchClient,  # Client that performs one search                    # search client
        log_factory: LogFactory,  # Factory used to obtain the structured logger                     # log factory
        max_candidates: int,  # Most candidates one search may hand back                             # candidate cap
    ) -> None:
        """Store the search client and the limit applied to every result.

        What this method is:
            - The constructor. It keeps its collaborators and the limit that bounds how many
              articles one search can put in front of the agent. It performs no network work.

        Why the limit is injected:
            - It trades answer quality against tokens and latency, so it belongs in configuration
              where it can be tuned against real searches rather than fixed in code.

        Args:
            search_client: The client that performs one search.
            log_factory: Factory used to obtain the structured logger.
            max_candidates: Most candidates one search may hand back.

        Returns:
            None.

        Example:
            >>> KnowledgeBaseSource(client, log_factory, 15)  # doctest: +SKIP
        """
        self._search_client = search_client  # Performs the search                                   # search client
        self._logger: StructuredLogger = log_factory.get_logger("servicenow_kb_source")  # Named logger  # logger
        self._max_candidates = max_candidates  # Cap on how many candidates go back                  # candidate cap

    # ============================================ Public API =====================================
    def search(self, query: str, correlation_id: str) -> list[dict[str, Any]]:
        """Return the candidate articles for one search description.

        What this method does:
            - Sends the agent's search description to the endpoint, then reduces and cleans each
              result so the agent receives only what it needs to choose between articles.

        Why the query matters:
            - The search text is written by the agent, which decides how to describe the user's
              problem. It is passed straight through, so the quality of the candidate set follows
              directly from how the agent phrases its search.

        Security and production notes:
            1. Results with no article number are dropped, since the agent could not resolve with
               one, and their count is logged.
            2. A search failure is raised rather than returned as an empty list.

        Args:
            query: The search description the agent asked for.
            correlation_id: The end-to-end correlation id for this turn.

        Returns:
            The candidate articles, each a dict of kb_id, title, category and content; possibly
            empty when nothing matched.

        Raises:
            KnowledgeBaseSourceError: If the search endpoint could not be reached or read.

        Example:
            >>> source.search("outlook is not working", "cid")  # doctest: +SKIP
            [{'kb_id': 'KB0024755', 'title': '...', 'category': '...', 'content': '...'}]
        """
        try:  # Ask the endpoint for the articles matching this search description                   # try search
            results = self._search_client.search(query, correlation_id)  # Run the search            # run search
        except ServiceNowSearchError as search_error:  # The endpoint could not be reached or read   # search failed
            raise KnowledgeBaseSourceError("The knowledge base could not be searched.") from search_error  # Domain error  # raise

        candidates: list[dict[str, Any]] = []  # The reduced candidates handed to the agent          # accumulator
        skipped_count = 0  # Results dropped because they carry no article number                    # skip counter
        for result in results:  # Reduce each result in turn                                         # each result
            candidate = self._candidate_from(result)  # Pull out the fields the agent needs          # reduce
            if candidate is None:  # No article number, so the agent could not resolve with it       # unusable?
                skipped_count += 1  # Count it and move on                                           # bump skip
                continue  # Leave it out of the candidate set                                        # skip result
            candidates.append(candidate)  # Keep this candidate                                      # keep
            if len(candidates) >= self._max_candidates:  # The cap for one search has been reached   # capped?
                break  # Stop reducing; the rest would only add tokens                               # stop

        if skipped_count:  # Only log the skip when at least one result was dropped                  # any skipped?
            self._logger.log(  # Note how many results carried no article number                     # log warning
                event="kb_results_skipped",  # Event name                                            # event
                correlation_id=correlation_id,  # Log key                                            # log key
                level="WARNING",  # Severity level                                                   # level
                skipped_count=skipped_count,  # How many were dropped                                # skipped count
            )

        self._logger.log(  # Log the shape of what the agent is about to receive                     # log info
            event="kb_candidates_returned",  # Event name for a candidate hand-off                   # event
            correlation_id=correlation_id,  # Log key                                                # log key
            result_count=len(results),  # How many results the search returned                       # result count
            candidate_count=len(candidates),  # How many candidates the agent receives               # candidate count
        )
        return candidates  # Hand the reduced candidate set back                                     # return candidates

    # ========================================= Internal helpers ==================================
    def _candidate_from(self, result: dict) -> Optional[dict[str, Any]]:
        """Reduce one search result to a candidate, or None when it carries no article number.

        What this method does:
            - Reads the four columns worth keeping, cleans their text, and assembles the small dict
              the agent receives. Anything else the result carries is left behind.

        Why the article number decides:
            - It is what the agent resolves with, so a result without one cannot become an answer
              and is worth no tokens.

        Args:
            result: One search result from the endpoint.

        Returns:
            The candidate dict, or None when the result carries no article number.

        Example:
            >>> source._candidate_from(result)  # doctest: +SKIP
            {'kb_id': 'KB0024755', 'title': '...', 'category': '...', 'content': '...'}
        """
        columns = result.get("columns") if isinstance(result, dict) else None  # The result's columns  # get columns
        if not isinstance(columns, list):  # A result with no columns carries nothing to read        # no columns?
            return None  # Cannot build a candidate                                                  # skip

        values: dict[str, str] = {}  # Column values, keyed by field name                            # value map
        for column in columns:  # Read each column once                                              # each column
            if not isinstance(column, dict):  # Skip anything that is not a field entry              # not a field?
                continue  # Move on                                                                  # skip
            field_name = column.get("fieldName")  # Which field this column holds                    # field name
            if field_name in (_TITLE_FIELD, _NUMBER_FIELD, _CONTENT_FIELD, _CATEGORY_FIELD):  # Wanted?  # wanted field?
                # displayValue carries the readable form where the two differ, as it does for the
                # category, whose value is an internal identifier.
                raw_value = column.get("displayValue") or column.get("value") or ""  # Readable form  # read value
                values[field_name] = str(raw_value)  # Keep it as text                               # store value

        kb_id = self._clean(values.get(_NUMBER_FIELD, "")).strip()  # The article number             # article number
        if not kb_id:  # Without a number the agent has nothing to resolve with                      # no number?
            return None  # Drop this result                                                          # skip

        return {  # The candidate the agent chooses from                                             # build candidate
            "kb_id": kb_id,  # What the agent resolves with                                          # article number
            "title": self._clean(values.get(_TITLE_FIELD, "")),  # The article's title line          # title
            "category": self._clean(values.get(_CATEGORY_FIELD, "")),  # Helps tell articles apart   # category
            "content": self._clean(values.get(_CONTENT_FIELD, "")),  # The article body              # content
        }

    @staticmethod  # Declare a static helper (needs neither instance nor class state)
    def _clean(text: str) -> str:
        """Return the text with search markup and escapes resolved.

        What this method does:
            - Strips the highlight markup the search service adds, turns escaped entities back into
              their characters, and collapses runs of whitespace.

        Why it exists:
            - The text is going into a model's input. Markup and repeated whitespace cost tokens and
              carry no meaning, and an escaped entity in the middle of a sentence reads as noise.

        Args:
            text: The raw text from a search result column.

        Returns:
            The cleaned text.

        Example:
            >>> KnowledgeBaseSource._clean("Mailbox <highlight>Not Working</highlight> &quot;now&quot;")
            'Mailbox Not Working "now"'
        """
        cleaned = _SEARCH_MARKUP_PATTERN.sub("", text or "")  # Take out the highlight markup        # strip markup
        cleaned = html.unescape(cleaned)  # Turn &quot; and friends back into characters             # unescape
        return _WHITESPACE_RUN_PATTERN.sub(" ", cleaned).strip()  # Collapse whitespace runs         # collapse
