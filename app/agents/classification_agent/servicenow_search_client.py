####################################################################################################
# Project name      : Outlook Support Classification Agent                                         #
# Business owner    : <fill: business owner / team>                                                #
# Notebook Author   : <fill: author name / team>                                                   #
# Date              : <fill: date>                                                                 #
#                                                                                                  #
# Purpose of file:                                                                                 #
# HTTP client for the ServiceNow knowledge search endpoint.                                        #
#   1. Send one GET per search, carrying the agent's search text and the configured parameters.    #
#   2. Authenticate with the bearer token, supplied from the environment and never logged.         #
#   3. Bound every call with a timeout and exponential-backoff retry on transient failures.        #
#   4. Return the article records as a list of dicts for the agent to select from.                 #
#                                                                                                  #
# Connection reuse:                                                                                #
#   One pooled requests.Session is held for the life of this object, which is built once per        #
#   worker. A fresh TCP and TLS handshake per search would burn the instance's outbound ports and  #
#   show up under load as random timeouts.                                                         #
#                                                                                                  #
# Source:-                                                                                         #
#   - requests supplies the pooled HTTP session and the transport error types worth retrying.      #
#   - backoff_retry supplies run_with_retry, the exponential-backoff runner.                       #
#   - telemetry_logging supplies LogFactory / StructuredLogger for the search events.              #
####################################################################################################

# ============================================ Imports =============================================
from __future__ import annotations  # Enable postponed evaluation of type annotations (PEP 563)     # future import

from typing import Any, Optional  # Type hints for record values and optional arguments             # stdlib typing

import requests  # Pooled HTTP session and the transport error types                                # http client
from requests.adapters import HTTPAdapter  # Lets the connection pool size be set explicitly        # pool adapter

from .backoff_retry import run_with_retry  # Exponential-backoff runner for transient failures       # backoff retry
from .telemetry_logging import LogFactory, StructuredLogger  # Logger factory + structured logger    # telemetry

# Status codes worth trying again: the service is rate-limiting us, or is briefly unhealthy.
_RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})  # Transient statuses  # retry statuses

# Where the result list sits inside the reply. The service nests it under the search it ran; the
# shorter paths are tried afterwards so a flatter reply is still read correctly.
_RECORD_LIST_PATHS: tuple[tuple[str, ...], ...] = (  # Paths tried in order                          # list paths
    ("result", "searchResult", "search", "searchResults"),  # The nesting the service uses           # full path
    ("searchResult", "search", "searchResults"),  # Without the outer result wrapper                 # shorter
    ("search", "searchResults"),  # Just the search block                                            # shorter
    ("searchResults",),  # At the top level                                                          # flat
    ("results",),  # A plainly named list                                                            # flat
)


# =========================================== Exceptions ==========================================
class ServiceNowSearchError(Exception):
    """Raised when the knowledge search endpoint cannot be reached or returns an unusable reply.

    What this class is:
        - The single error this client raises. Every transport failure, error status and malformed
          reply is mapped to it, so the caller reasons about one type.

    Why this exists:
        - To keep the boundary clean and the message generic, so no URL, token or upstream error
          body can travel back to the caller and reach an end user.

    Example:
        >>> raise ServiceNowSearchError("The knowledge search could not be completed.")  # doctest: +SKIP
    """


class _RetryableSearchResponse(Exception):
    """Internal signal that a response carried a status worth trying again.

    What this class is:
        - A private marker raised inside the retry runner so a 429 or 5xx is retried the same way a
          dropped connection is, then reported as a normal failure once the attempts run out.
    """


# ======================================== Search client ==========================================
class ServiceNowSearchClient:
    """Fetch candidate knowledge articles for one search description.

    What this class is:
        - The HTTP boundary to the knowledge search endpoint. It owns the pooled session, the
          request parameters, the timeout and retry policy, and the reading of the reply.

    Why this exists:
        - To keep every detail of the remote call - URL shape, authentication, retries, response
          envelope - in one place, so the knowledge-base source above it deals only in records.

    Security and production notes:
        1. The bearer token is held in memory and sent as a request header. It is never written to
           a log line, and the response body is never logged either.
        2. The search text is untrusted: it originates from a model. It is passed as a query
           parameter so the HTTP layer encodes it, and is never concatenated into the URL.
        3. Every call is bounded by a timeout and a capped number of attempts, so a slow or
           unhealthy endpoint cannot hold a chat turn open indefinitely.

    Example:
        >>> client.search("outlook hanging", "cid")  # doctest: +SKIP
        [{'sysId': '...', 'title': '...', 'columns': [...]}, ...]
    """

    def __init__(  # Configure the endpoint, the credentials, and the call policy
        self,
        base_url: str,  # Endpoint URL, without a query string                                       # base url
        bearer_token: str,  # Token sent as "Authorization: Bearer <token>"                          # token
        registration_id: str,  # Identifies this integration to the service                          # registration
        user_id: str,  # The account the search runs as                                              # user id
        req_type: str,  # Fixed 'reqType' query parameter                                            # req type
        search_type: str,  # Fixed 'searchType' query parameter                                      # search type
        request_timeout_seconds: int,  # Per-call timeout in seconds                                 # timeout
        pool_maxsize: int,  # Outbound connections kept alive for reuse                              # pool size
        verify_tls: bool,  # Whether the server certificate is verified                              # verify tls
        log_factory: LogFactory,  # Factory used to obtain a structured logger                       # log factory
        retry_max_attempts: int,  # Maximum attempts per search                                      # attempts
        retry_base_delay_seconds: float,  # Base delay for exponential backoff                       # base delay
        retry_max_delay_seconds: float,  # Upper bound on a single backoff wait                      # max delay
    ) -> None:
        """Store the call policy and build the pooled session. Performs no network work.

        What this method is:
            - The constructor: it keeps the endpoint configuration and prepares one reusable
              session. No request is sent here.

        Why this exists:
            - So the client can be built once per worker and the connection pool shared by every
              search, rather than a new connection being opened per turn.

        Security and production notes:
            1. The token is stored on the instance and set on the session's default headers, so it
               is sent on every request without appearing at any call site.
            2. verify_tls should stay True. Turning it off disables certificate checking and is
               only ever appropriate against a test endpoint behind a private network.

        Args:
            base_url: The endpoint URL, without a query string.
            bearer_token: The token sent in the Authorization header.
            registration_id: Identifies this integration to the service.
            user_id: The account the search runs as.
            req_type: The fixed 'reqType' query parameter.
            search_type: The fixed 'searchType' query parameter.
            request_timeout_seconds: Per-call timeout in seconds.
            pool_maxsize: Outbound connections kept alive for reuse.
            verify_tls: Whether the server certificate is verified.
            log_factory: Factory used to obtain a structured logger.
            retry_max_attempts: Maximum attempts per search.
            retry_base_delay_seconds: Base backoff delay.
            retry_max_delay_seconds: Upper bound on a single backoff wait.

        Returns:
            None.

        Example:
            >>> ServiceNowSearchClient("https://host/path", "tok", "reg", "a@b.com",  # doctest: +SKIP
            ...     "search", "knowledge", 20, 10, True, log_factory, 3, 0.5, 8.0)
        """
        self._base_url = base_url  # Endpoint the search is sent to                                  # base url
        self._registration_id = registration_id  # Sent on every request                             # registration
        self._user_id = user_id  # Sent on every request                                             # user id
        self._req_type = req_type  # Sent on every request                                           # req type
        self._search_type = search_type  # Sent on every request                                     # search type
        self._request_timeout_seconds = request_timeout_seconds  # Per-call timeout                  # timeout
        self._verify_tls = verify_tls  # Certificate verification switch                             # verify tls
        self._logger: StructuredLogger = log_factory.get_logger("servicenow_search_client")  # Named logger  # logger
        self._retry_max_attempts = retry_max_attempts  # Maximum attempts per search                 # attempts
        self._retry_base_delay_seconds = retry_base_delay_seconds  # Base backoff delay              # base delay
        self._retry_max_delay_seconds = retry_max_delay_seconds  # Backoff ceiling                   # max delay

        self._session = requests.Session()  # One pooled session reused by every search              # build session
        self._session.headers.update({  # Headers sent on every request from this session            # set headers
            "Authorization": f"Bearer {bearer_token}",  # Bearer authentication                      # auth header
            "Accept": "application/json",  # Ask for the JSON representation                         # accept
        })
        adapter = HTTPAdapter(pool_connections=pool_maxsize, pool_maxsize=pool_maxsize)  # Sized pool  # build adapter
        self._session.mount("https://", adapter)  # Reuse connections for HTTPS                      # mount https
        self._session.mount("http://", adapter)  # And for plain HTTP, if ever used                  # mount http

    # ============================================ Public API =====================================
    def search(self, search_text: str, correlation_id: str) -> list[dict[str, Any]]:
        """Run one knowledge search and return the article records.

        What this method is:
            - The single public operation: it sends the agent's search description to the endpoint
              and hands back whatever articles came out of the reply.

        Why this exists:
            - The agent asks for a search and then chooses from what it is given. This method is the
              step in between, and the only place the remote service is contacted.

        Security and production notes:
            1. The search text is sent as a query parameter so it is encoded by the HTTP layer, and
               only its length is logged - never the text itself, which may carry user detail.
            2. Failures are logged with the status code and re-raised as ServiceNowSearchError, so
               no response body or URL reaches the caller.

        Args:
            search_text: The search description the agent asked for.
            correlation_id: The end-to-end correlation id for this turn.

        Returns:
            The article records, each a dict; possibly empty when nothing matched.

        Raises:
            ServiceNowSearchError: If the search cannot be completed after the configured attempts.

        Example:
            >>> client.search("outlook hanging", "cid")  # doctest: +SKIP
            [{'sysId': '...', 'title': '...'}, ...]
        """
        query_parameters = {  # The query string this endpoint expects                               # build params
            "reqType": self._req_type,  # Fixed request type                                        # req type
            "registrationId": self._registration_id,  # Identifies this integration                 # registration
            "searchText": search_text,  # The agent's search description for this turn              # search text
            "userID": self._user_id,  # The account the search runs as                              # user id
            "searchType": self._search_type,  # Fixed search type                                   # search type
        }

        def _send_request() -> "requests.Response":  # One attempt, retried by the runner below      # one attempt
            """Send one GET and raise when the status is worth trying again."""
            response = self._session.get(  # Send the search request                                # send get
                self._base_url,  # Endpoint URL                                                     # url
                params=query_parameters,  # Encoded by requests, so the search text is safe          # params
                timeout=self._request_timeout_seconds,  # Bound this attempt                        # timeout
                verify=self._verify_tls,  # Certificate verification                                 # verify
            )
            if response.status_code in _RETRYABLE_STATUS_CODES:  # Rate-limited or briefly unhealthy  # retryable?
                raise _RetryableSearchResponse(f"status {response.status_code}")  # Signal a retry   # signal retry
            return response  # Hand the response back for reading                                   # return response

        try:  # Run the request through the retry runner and normalise any failure                  # guarded run
            response = run_with_retry(  # Retry on transport failures and retryable statuses        # retry call
                _send_request,  # The attempt to execute                                            # operation
                correlation_id=correlation_id,  # Correlation id for retry logs                     # correlation
                operation_name="servicenow_search",  # Operation label for logs                     # op name
                logger=self._logger,  # Structured logger for retry events                          # logger
                max_attempts=self._retry_max_attempts,  # Attempt cap from config                   # attempts
                base_delay_seconds=self._retry_base_delay_seconds,  # Base backoff delay            # base delay
                max_delay_seconds=self._retry_max_delay_seconds,  # Backoff ceiling                 # max delay
                retryable_exceptions=(  # What triggers another attempt                             # retryable set
                    requests.ConnectionError,  # Connection refused, DNS failure, reset             # conn error
                    requests.Timeout,  # Connect or read timeout                                    # timeout
                    _RetryableSearchResponse,  # A 429 or 5xx status                                # bad status
                ),
            )
        except (requests.RequestException, _RetryableSearchResponse) as request_error:  # Out of attempts  # failed
            self._logger.log(  # Log the failure without the URL or any response body               # log failure
                event="kb_search_request_failed",  # Event name                                      # event
                correlation_id=correlation_id,  # Log key                                            # log key
                level="ERROR",  # Severity level                                                     # level
                error_type=type(request_error).__name__,  # Exception class name                     # error type
                error_message=str(request_error),  # Exception message                               # error msg
            )
            raise ServiceNowSearchError("The knowledge search could not be completed.") from request_error  # Domain error  # raise

        if response.status_code >= 400:  # A non-retryable error status: auth, bad request, not found  # error status?
            self._logger.log(  # Log the status so a misconfiguration is visible                    # log status
                event="kb_search_rejected",  # Event name                                            # event
                correlation_id=correlation_id,  # Log key                                            # log key
                level="ERROR",  # Severity level                                                     # level
                status_code=response.status_code,  # The status the service returned                 # status
            )
            raise ServiceNowSearchError("The knowledge search was rejected.")  # Domain error         # raise

        records = self._records_from(response, correlation_id)  # Read the article list out of the reply  # read records
        self._logger.log(  # Log the outcome, by shape only                                          # log result
            event="kb_search_completed",  # Event name                                               # event
            correlation_id=correlation_id,  # Log key                                                # log key
            status_code=response.status_code,  # The status the service returned                     # status
            search_text_length=len(search_text),  # Length only; the text itself is never logged     # text length
            record_count=len(records),  # How many articles came back                                # record count
        )
        return records  # Hand the records to the caller                                            # return records

    # ========================================= Internal helpers ==================================
    def _records_from(self, response: "requests.Response", correlation_id: str) -> list[dict[str, Any]]:
        """Read the article list out of the response body.

        What this method is:
            - The reply reader. It decodes the JSON body and finds the list of article records
              inside whichever envelope the service used.

        Why this exists:
            - It is the one place the response shape is interpreted, so pointing this client at a
              different envelope is a change here and nowhere else.

        Security and production notes:
            1. An undecodable or unexpected body is reported as a failure rather than being passed
               on as an empty result, so a contract change cannot quietly look like "no articles
               matched" and route every conversation to a human.
            2. When no known envelope key matches, the body's top-level keys are logged - the key
               names only, never their values - so the real shape can be read off the logs.

        Args:
            response: The successful HTTP response.
            correlation_id: The end-to-end correlation id.

        Returns:
            The article records as a list of dicts.

        Raises:
            ServiceNowSearchError: If the body cannot be decoded or holds no recognisable list.

        Example:
            >>> client._records_from(response, "cid")  # doctest: +SKIP
            [{'sysId': '...'}]
        """
        try:  # Decode the response body as JSON                                                     # decode json
            body = response.json()  # Parse the reply                                                # parse
        except ValueError as decode_error:  # The body was not JSON at all                           # bad json
            self._logger.log(  # Log the decode failure                                              # log failure
                event="kb_search_response_undecodable",  # Event name                                # event
                correlation_id=correlation_id,  # Log key                                            # log key
                level="ERROR",  # Severity level                                                     # level
            )
            raise ServiceNowSearchError("The knowledge search reply could not be read.") from decode_error  # Domain error  # raise

        if isinstance(body, list):  # The service returned the records as the whole body             # bare list?
            return [record for record in body if isinstance(record, dict)]  # Keep the dict records  # filter dicts

        for path in _RECORD_LIST_PATHS:  # Walk each known location in turn                          # each path
            found = self._walk(body, path)  # Follow this path through the reply                     # walk path
            if isinstance(found, list):  # Landed on the list of records                             # is a list?
                return [record for record in found if isinstance(record, dict)]  # Keep dicts        # filter dicts

        self._logger.log(  # Nothing matched: log the shape so the location can be added             # log shape
            event="kb_search_response_unrecognised",  # Event name                                   # event
            correlation_id=correlation_id,  # Log key                                                # log key
            level="ERROR",  # Severity level                                                         # level
            body_type=type(body).__name__,  # Whether it was an object, list or scalar               # body type
            top_level_keys=sorted(body.keys()) if isinstance(body, dict) else [],  # Key names only  # keys
        )
        raise ServiceNowSearchError("The knowledge search reply had an unexpected shape.")  # Domain error  # raise

    @staticmethod  # Declare a static helper (needs neither instance nor class state)
    def _walk(body: Any, path: tuple[str, ...]) -> Any:
        """Follow a sequence of keys through the reply, returning None if any step is missing.

        Args:
            body: The decoded reply, or any value reached part-way along the path.
            path: The keys to follow, outermost first.

        Returns:
            Whatever sits at the end of the path, or None if the path does not exist.

        Example:
            >>> ServiceNowSearchClient._walk({"a": {"b": [1]}}, ("a", "b"))
            [1]
        """
        current = body  # Start at the top of the reply                                              # start
        for key in path:  # Follow each key in turn                                                  # each key
            if not isinstance(current, dict):  # Cannot go deeper into a non-object                  # dead end?
                return None  # This path does not exist                                              # signal none
            current = current.get(key)  # Step into the next level                                   # step
        return current  # Whatever the path led to                                                   # return value

    def close(self) -> None:
        """Close the pooled session. Best-effort, and safe to call more than once.

        Args:
            None.

        Returns:
            None.

        Example:
            >>> client.close()  # doctest: +SKIP
        """
        try:  # Release the pooled connections                                                       # try close
            self._session.close()  # Close the session                                               # close
        except Exception:  # Teardown must never raise                                               # guard
            pass  # Nothing useful to do if closing fails                                            # ignore
