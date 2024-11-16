import uuid
import random
import time
import warnings
from datetime import datetime, timedelta, timezone

from opentelemetry import trace as otel_trace, context
from opentelemetry.trace import (
    format_trace_id,
    format_span_id,
    Span as OtelSpan,
    TraceState,
    get_current_span,
    INVALID_SPAN,
)
from opentelemetry.trace.status import StatusCode
from opentelemetry.sdk.trace import ReadableSpan

import sentry_sdk
from sentry_sdk.consts import SPANSTATUS, SPANDATA
from sentry_sdk.profiler.continuous_profiler import get_profiler_id
from sentry_sdk.utils import (
    _serialize_span_attribute,
    get_current_thread_meta,
    is_valid_sample_rate,
    logger,
)

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, MutableMapping
    from typing import Any
    from typing import Dict
    from typing import Iterator
    from typing import List
    from typing import Optional
    from typing import overload
    from typing import ParamSpec
    from typing import Tuple
    from typing import Union
    from typing import TypeVar

    from typing_extensions import TypedDict, Unpack

    from opentelemetry.utils import types as OTelSpanAttributes

    P = ParamSpec("P")
    R = TypeVar("R")

    import sentry_sdk.profiler
    from sentry_sdk._types import (
        Event,
        MeasurementUnit,
        SamplingContext,
        MeasurementValue,
    )

    class SpanKwargs(TypedDict, total=False):
        trace_id: str
        """
        The trace ID of the root span. If this new span is to be the root span,
        omit this parameter, and a new trace ID will be generated.
        """

        span_id: str
        """The span ID of this span. If omitted, a new span ID will be generated."""

        parent_span_id: str
        """The span ID of the parent span, if applicable."""

        same_process_as_parent: bool
        """Whether this span is in the same process as the parent span."""

        sampled: bool
        """
        Whether the span should be sampled. Overrides the default sampling decision
        for this span when provided.
        """

        op: str
        """
        The span's operation. A list of recommended values is available here:
        https://develop.sentry.dev/sdk/performance/span-operations/
        """

        description: str
        """A description of what operation is being performed within the span. This argument is DEPRECATED. Please use the `name` parameter, instead."""

        status: str
        """The span's status. Possible values are listed at https://develop.sentry.dev/sdk/event-payloads/span/"""

        containing_transaction: Optional["Transaction"]
        """The transaction that this span belongs to."""

        start_timestamp: Optional[Union[datetime, float]]
        """
        The timestamp when the span started. If omitted, the current time
        will be used.
        """

        scope: "sentry_sdk.Scope"
        """The scope to use for this span. If not provided, we use the current scope."""

        origin: Optional[str]
        """
        The origin of the span.
        See https://develop.sentry.dev/sdk/performance/trace-origin/
        Default "manual".
        """

        name: str
        """A string describing what operation is being performed within the span/transaction."""

    class TransactionKwargs(SpanKwargs, total=False):
        source: str
        """
        A string describing the source of the transaction name. This will be used to determine the transaction's type.
        See https://develop.sentry.dev/sdk/event-payloads/transaction/#transaction-annotations for more information.
        Default "custom".
        """

        parent_sampled: bool
        """Whether the parent transaction was sampled. If True this transaction will be kept, if False it will be discarded."""

        baggage: "Baggage"
        """The W3C baggage header value. (see https://www.w3.org/TR/baggage/)"""

    ProfileContext = TypedDict(
        "ProfileContext",
        {
            "profiler_id": str,
        },
    )

BAGGAGE_HEADER_NAME = "baggage"
SENTRY_TRACE_HEADER_NAME = "sentry-trace"

# Transaction source
# see https://develop.sentry.dev/sdk/event-payloads/transaction/#transaction-annotations
TRANSACTION_SOURCE_CUSTOM = "custom"
TRANSACTION_SOURCE_URL = "url"
TRANSACTION_SOURCE_ROUTE = "route"
TRANSACTION_SOURCE_VIEW = "view"
TRANSACTION_SOURCE_COMPONENT = "component"
TRANSACTION_SOURCE_TASK = "task"

# These are typically high cardinality and the server hates them
LOW_QUALITY_TRANSACTION_SOURCES = [
    TRANSACTION_SOURCE_URL,
]

SOURCE_FOR_STYLE = {
    "endpoint": TRANSACTION_SOURCE_COMPONENT,
    "function_name": TRANSACTION_SOURCE_COMPONENT,
    "handler_name": TRANSACTION_SOURCE_COMPONENT,
    "method_and_path_pattern": TRANSACTION_SOURCE_ROUTE,
    "path": TRANSACTION_SOURCE_URL,
    "route_name": TRANSACTION_SOURCE_COMPONENT,
    "route_pattern": TRANSACTION_SOURCE_ROUTE,
    "uri_template": TRANSACTION_SOURCE_ROUTE,
    "url": TRANSACTION_SOURCE_ROUTE,
}

DEFAULT_SPAN_ORIGIN = "manual"

tracer = otel_trace.get_tracer(__name__)


def get_span_status_from_http_code(http_status_code):
    # type: (int) -> str
    """
    Returns the Sentry status corresponding to the given HTTP status code.

    See: https://develop.sentry.dev/sdk/event-payloads/contexts/#trace-context
    """
    if http_status_code < 400:
        return SPANSTATUS.OK

    elif 400 <= http_status_code < 500:
        if http_status_code == 403:
            return SPANSTATUS.PERMISSION_DENIED
        elif http_status_code == 404:
            return SPANSTATUS.NOT_FOUND
        elif http_status_code == 429:
            return SPANSTATUS.RESOURCE_EXHAUSTED
        elif http_status_code == 413:
            return SPANSTATUS.FAILED_PRECONDITION
        elif http_status_code == 401:
            return SPANSTATUS.UNAUTHENTICATED
        elif http_status_code == 409:
            return SPANSTATUS.ALREADY_EXISTS
        else:
            return SPANSTATUS.INVALID_ARGUMENT

    elif 500 <= http_status_code < 600:
        if http_status_code == 504:
            return SPANSTATUS.DEADLINE_EXCEEDED
        elif http_status_code == 501:
            return SPANSTATUS.UNIMPLEMENTED
        elif http_status_code == 503:
            return SPANSTATUS.UNAVAILABLE
        else:
            return SPANSTATUS.INTERNAL_ERROR

    return SPANSTATUS.UNKNOWN_ERROR


class _SpanRecorder:
    """Limits the number of spans recorded in a transaction."""

    __slots__ = ("maxlen", "spans")

    def __init__(self, maxlen):
        # type: (int) -> None
        # FIXME: this is `maxlen - 1` only to preserve historical behavior
        # enforced by tests.
        # Either this should be changed to `maxlen` or the JS SDK implementation
        # should be changed to match a consistent interpretation of what maxlen
        # limits: either transaction+spans or only child spans.
        self.maxlen = maxlen - 1
        self.spans = []  # type: List[Span]

    def add(self, span):
        # type: (Span) -> None
        if len(self.spans) > self.maxlen:
            span._span_recorder = None
        else:
            self.spans.append(span)


class Span:
    """A span holds timing information of a block of code.
    Spans can have multiple child spans thus forming a span tree.

    :param trace_id: The trace ID of the root span. If this new span is to be the root span,
        omit this parameter, and a new trace ID will be generated.
    :param span_id: The span ID of this span. If omitted, a new span ID will be generated.
    :param parent_span_id: The span ID of the parent span, if applicable.
    :param same_process_as_parent: Whether this span is in the same process as the parent span.
    :param sampled: Whether the span should be sampled. Overrides the default sampling decision
        for this span when provided.
    :param op: The span's operation. A list of recommended values is available here:
        https://develop.sentry.dev/sdk/performance/span-operations/
    :param description: A description of what operation is being performed within the span.

        .. deprecated:: 2.15.0
            Please use the `name` parameter, instead.
    :param name: A string describing what operation is being performed within the span.
    :param hub: The hub to use for this span.

        .. deprecated:: 2.0.0
            Please use the `scope` parameter, instead.
    :param status: The span's status. Possible values are listed at
        https://develop.sentry.dev/sdk/event-payloads/span/
    :param containing_transaction: The transaction that this span belongs to.
    :param start_timestamp: The timestamp when the span started. If omitted, the current time
        will be used.
    :param scope: The scope to use for this span. If not provided, we use the current scope.
    """

    __slots__ = (
        "trace_id",
        "span_id",
        "parent_span_id",
        "same_process_as_parent",
        "sampled",
        "op",
        "description",
        "_measurements",
        "start_timestamp",
        "_start_timestamp_monotonic_ns",
        "status",
        "timestamp",
        "_tags",
        "_data",
        "_span_recorder",
        "_context_manager_state",
        "_containing_transaction",
        "scope",
        "origin",
        "name",
    )

    def __init__(
        self,
        trace_id=None,  # type: Optional[str]
        span_id=None,  # type: Optional[str]
        parent_span_id=None,  # type: Optional[str]
        same_process_as_parent=True,  # type: bool
        sampled=None,  # type: Optional[bool]
        op=None,  # type: Optional[str]
        description=None,  # type: Optional[str]
        status=None,  # type: Optional[str]
        containing_transaction=None,  # type: Optional[Transaction]
        start_timestamp=None,  # type: Optional[Union[datetime, float]]
        scope=None,  # type: Optional[sentry_sdk.Scope]
        origin=None,  # type: Optional[str]
        name=None,  # type: Optional[str]
    ):
        # type: (...) -> None
        self.trace_id = trace_id or uuid.uuid4().hex
        self.span_id = span_id or uuid.uuid4().hex[16:]
        self.parent_span_id = parent_span_id
        self.same_process_as_parent = same_process_as_parent
        self.sampled = sampled
        self.op = op
        self.description = name or description
        self.status = status
        self.scope = scope
        self.origin = origin or DEFAULT_SPAN_ORIGIN
        self._measurements = {}  # type: Dict[str, MeasurementValue]
        self._tags = {}  # type: MutableMapping[str, str]
        self._data = {}  # type: Dict[str, Any]
        self._containing_transaction = containing_transaction

        if start_timestamp is None:
            start_timestamp = datetime.now(timezone.utc)
        elif isinstance(start_timestamp, float):
            start_timestamp = datetime.fromtimestamp(start_timestamp, timezone.utc)
        self.start_timestamp = start_timestamp
        try:
            # profiling depends on this value and requires that
            # it is measured in nanoseconds
            self._start_timestamp_monotonic_ns = time.perf_counter_ns()
        except AttributeError:
            pass

        #: End timestamp of span
        self.timestamp = None  # type: Optional[datetime]

        self._span_recorder = None  # type: Optional[_SpanRecorder]

        self.update_active_thread()
        self.set_profiler_id(get_profiler_id())

    # TODO this should really live on the Transaction class rather than the Span
    # class
    def init_span_recorder(self, maxlen):
        # type: (int) -> None
        if self._span_recorder is None:
            self._span_recorder = _SpanRecorder(maxlen)

    def __repr__(self):
        # type: () -> str
        return (
            "<%s(op=%r, description:%r, trace_id=%r, span_id=%r, parent_span_id=%r, sampled=%r, origin=%r)>"
            % (
                self.__class__.__name__,
                self.op,
                self.description,
                self.trace_id,
                self.span_id,
                self.parent_span_id,
                self.sampled,
                self.origin,
            )
        )

    def __enter__(self):
        # type: () -> Span
        scope = self.scope or sentry_sdk.get_current_scope()
        old_span = scope.span
        scope.span = self
        self._context_manager_state = (scope, old_span)
        return self

    def __exit__(self, ty, value, tb):
        # type: (Optional[Any], Optional[Any], Optional[Any]) -> None
        if value is not None:
            self.set_status(SPANSTATUS.INTERNAL_ERROR)

        scope, old_span = self._context_manager_state
        del self._context_manager_state
        self.finish(scope)
        scope.span = old_span

    @property
    def containing_transaction(self):
        # type: () -> Optional[Transaction]
        """The ``Transaction`` that this span belongs to.
        The ``Transaction`` is the root of the span tree,
        so one could also think of this ``Transaction`` as the "root span"."""

        # this is a getter rather than a regular attribute so that transactions
        # can return `self` here instead (as a way to prevent them circularly
        # referencing themselves)
        return self._containing_transaction

    def start_child(self, **kwargs):
        # type: (**Any) -> Span
        """
        Start a sub-span from the current span or transaction.

        Takes the same arguments as the initializer of :py:class:`Span`. The
        trace id, sampling decision, transaction pointer, and span recorder are
        inherited from the current span/transaction.

        The instrumenter parameter is deprecated for user code, and it will
        be removed in the next major version. Going forward, it should only
        be used by the SDK itself.
        """
        if kwargs.get("description") is not None:
            warnings.warn(
                "The `description` parameter is deprecated. Please use `name` instead.",
                DeprecationWarning,
                stacklevel=2,
            )

        kwargs.setdefault("sampled", self.sampled)

        child = Span(
            trace_id=self.trace_id,
            parent_span_id=self.span_id,
            containing_transaction=self.containing_transaction,
            **kwargs,
        )

        span_recorder = (
            self.containing_transaction and self.containing_transaction._span_recorder
        )
        if span_recorder:
            span_recorder.add(child)

        return child

    @classmethod
    def continue_from_environ(
        cls,
        environ,  # type: Mapping[str, str]
        **kwargs,  # type: Any
    ):
        # type: (...) -> Transaction
        """
        Create a Transaction with the given params, then add in data pulled from
        the ``sentry-trace`` and ``baggage`` headers from the environ (if any)
        before returning the Transaction.

        This is different from :py:meth:`~sentry_sdk.tracing.Span.continue_from_headers`
        in that it assumes header names in the form ``HTTP_HEADER_NAME`` -
        such as you would get from a WSGI/ASGI environ -
        rather than the form ``header-name``.

        :param environ: The ASGI/WSGI environ to pull information from.
        """
        if cls is Span:
            logger.warning(
                "Deprecated: use Transaction.continue_from_environ "
                "instead of Span.continue_from_environ."
            )
        return Transaction.continue_from_headers(EnvironHeaders(environ), **kwargs)

    @classmethod
    def continue_from_headers(
        cls,
        headers,  # type: Mapping[str, str]
        **kwargs,  # type: Any
    ):
        # type: (...) -> Transaction
        """
        Create a transaction with the given params (including any data pulled from
        the ``sentry-trace`` and ``baggage`` headers).

        :param headers: The dictionary with the HTTP headers to pull information from.
        """
        # TODO move this to the Transaction class
        if cls is Span:
            logger.warning(
                "Deprecated: use Transaction.continue_from_headers "
                "instead of Span.continue_from_headers."
            )

        # TODO-neel move away from this kwargs stuff, it's confusing and opaque
        # make more explicit
        baggage = Baggage.from_incoming_header(headers.get(BAGGAGE_HEADER_NAME))
        kwargs.update({BAGGAGE_HEADER_NAME: baggage})

        sentrytrace_kwargs = extract_sentrytrace_data(
            headers.get(SENTRY_TRACE_HEADER_NAME)
        )

        if sentrytrace_kwargs is not None:
            kwargs.update(sentrytrace_kwargs)

            # If there's an incoming sentry-trace but no incoming baggage header,
            # for instance in traces coming from older SDKs,
            # baggage will be empty and immutable and won't be populated as head SDK.
            baggage.freeze()

        transaction = Transaction(**kwargs)
        transaction.same_process_as_parent = False

        return transaction

    def iter_headers(self):
        # type: () -> Iterator[Tuple[str, str]]
        """
        Creates a generator which returns the span's ``sentry-trace`` and ``baggage`` headers.
        If the span's containing transaction doesn't yet have a ``baggage`` value,
        this will cause one to be generated and stored.
        """
        if not self.containing_transaction:
            # Do not propagate headers if there is no containing transaction. Otherwise, this
            # span ends up being the root span of a new trace, and since it does not get sent
            # to Sentry, the trace will be missing a root transaction. The dynamic sampling
            # context will also be missing, breaking dynamic sampling & traces.
            return

        yield SENTRY_TRACE_HEADER_NAME, self.to_traceparent()

        baggage = self.containing_transaction.get_baggage().serialize()
        if baggage:
            yield BAGGAGE_HEADER_NAME, baggage

    @classmethod
    def from_traceparent(
        cls,
        traceparent,  # type: Optional[str]
        **kwargs,  # type: Any
    ):
        # type: (...) -> Optional[Transaction]
        """
        DEPRECATED: Use :py:meth:`sentry_sdk.tracing.Span.continue_from_headers`.

        Create a ``Transaction`` with the given params, then add in data pulled from
        the given ``sentry-trace`` header value before returning the ``Transaction``.
        """
        logger.warning(
            "Deprecated: Use Transaction.continue_from_headers(headers, **kwargs) "
            "instead of from_traceparent(traceparent, **kwargs)"
        )

        if not traceparent:
            return None

        return cls.continue_from_headers(
            {SENTRY_TRACE_HEADER_NAME: traceparent}, **kwargs
        )

    def to_traceparent(self):
        # type: () -> str
        if self.sampled is True:
            sampled = "1"
        elif self.sampled is False:
            sampled = "0"
        else:
            sampled = None

        traceparent = "%s-%s" % (self.trace_id, self.span_id)
        if sampled is not None:
            traceparent += "-%s" % (sampled,)

        return traceparent

    def to_baggage(self):
        # type: () -> Optional[Baggage]
        """Returns the :py:class:`~sentry_sdk.tracing_utils.Baggage`
        associated with this ``Span``, if any. (Taken from the root of the span tree.)
        """
        if self.containing_transaction:
            return self.containing_transaction.get_baggage()
        return None

    def set_tag(self, key, value):
        # type: (str, Any) -> None
        self._tags[key] = value

    def set_data(self, key, value):
        # type: (str, Any) -> None
        self._data[key] = value

    def set_status(self, value):
        # type: (str) -> None
        self.status = value

    def set_measurement(self, name, value, unit=""):
        # type: (str, float, MeasurementUnit) -> None
        self._measurements[name] = {"value": value, "unit": unit}

    def set_thread(self, thread_id, thread_name):
        # type: (Optional[int], Optional[str]) -> None

        if thread_id is not None:
            self.set_data(SPANDATA.THREAD_ID, str(thread_id))

            if thread_name is not None:
                self.set_data(SPANDATA.THREAD_NAME, thread_name)

    def set_profiler_id(self, profiler_id):
        # type: (Optional[str]) -> None
        if profiler_id is not None:
            self.set_data(SPANDATA.PROFILER_ID, profiler_id)

    def set_http_status(self, http_status):
        # type: (int) -> None
        self.set_tag(
            "http.status_code", str(http_status)
        )  # we keep this for backwards compatibility
        self.set_data(SPANDATA.HTTP_STATUS_CODE, http_status)
        self.set_status(get_span_status_from_http_code(http_status))

    def is_success(self):
        # type: () -> bool
        return self.status == "ok"

    def finish(self, scope=None, end_timestamp=None):
        # type: (Optional[sentry_sdk.Scope], Optional[Union[float, datetime]]) -> Optional[str]
        """
        Sets the end timestamp of the span.

        Additionally it also creates a breadcrumb from the span,
        if the span represents a database or HTTP request.

        :param scope: The scope to use for this transaction.
            If not provided, the current scope will be used.
        :param end_timestamp: Optional timestamp that should
            be used as timestamp instead of the current time.

        :return: Always ``None``. The type is ``Optional[str]`` to match
            the return value of :py:meth:`sentry_sdk.tracing.Transaction.finish`.
        """
        if self.timestamp is not None:
            # This span is already finished, ignore.
            return None

        try:
            if end_timestamp:
                if isinstance(end_timestamp, float):
                    end_timestamp = datetime.fromtimestamp(end_timestamp, timezone.utc)
                self.timestamp = end_timestamp
            else:
                elapsed = time.perf_counter_ns() - self._start_timestamp_monotonic_ns
                self.timestamp = self.start_timestamp + timedelta(
                    microseconds=elapsed / 1000
                )
        except AttributeError:
            self.timestamp = datetime.now(timezone.utc)

        scope = scope or sentry_sdk.get_current_scope()
        maybe_create_breadcrumbs_from_span(scope, self)

        return None

    def to_json(self):
        # type: () -> Dict[str, Any]
        """Returns a JSON-compatible representation of the span."""

        rv = {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "same_process_as_parent": self.same_process_as_parent,
            "op": self.op,
            "description": self.description,
            "start_timestamp": self.start_timestamp,
            "timestamp": self.timestamp,
            "origin": self.origin,
        }  # type: Dict[str, Any]

        if self.status:
            self._tags["status"] = self.status

        if len(self._measurements) > 0:
            rv["measurements"] = self._measurements

        tags = self._tags
        if tags:
            rv["tags"] = tags

        data = self._data
        if data:
            rv["data"] = data

        return rv

    def get_trace_context(self):
        # type: () -> Any
        rv = {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "op": self.op,
            "description": self.description,
            "origin": self.origin,
        }  # type: Dict[str, Any]
        if self.status:
            rv["status"] = self.status

        if self.containing_transaction:
            rv["dynamic_sampling_context"] = (
                self.containing_transaction.get_baggage().dynamic_sampling_context()
            )

        data = {}

        thread_id = self._data.get(SPANDATA.THREAD_ID)
        if thread_id is not None:
            data["thread.id"] = thread_id

        thread_name = self._data.get(SPANDATA.THREAD_NAME)
        if thread_name is not None:
            data["thread.name"] = thread_name

        if data:
            rv["data"] = data

        return rv

    def get_profile_context(self):
        # type: () -> Optional[ProfileContext]
        profiler_id = self._data.get(SPANDATA.PROFILER_ID)
        if profiler_id is None:
            return None

        return {
            "profiler_id": profiler_id,
        }

    def update_active_thread(self):
        # type: () -> None
        thread_id, thread_name = get_current_thread_meta()
        self.set_thread(thread_id, thread_name)


class Transaction(Span):
    """The Transaction is the root element that holds all the spans
    for Sentry performance instrumentation.

    :param name: Identifier of the transaction.
        Will show up in the Sentry UI.
    :param parent_sampled: Whether the parent transaction was sampled.
        If True this transaction will be kept, if False it will be discarded.
    :param baggage: The W3C baggage header value.
        (see https://www.w3.org/TR/baggage/)
    :param source: A string describing the source of the transaction name.
        This will be used to determine the transaction's type.
        See https://develop.sentry.dev/sdk/event-payloads/transaction/#transaction-annotations
        for more information. Default "custom".
    :param kwargs: Additional arguments to be passed to the Span constructor.
        See :py:class:`sentry_sdk.tracing.Span` for available arguments.
    """

    __slots__ = (
        "name",
        "source",
        "parent_sampled",
        # used to create baggage value for head SDKs in dynamic sampling
        "sample_rate",
        "_measurements",
        "_contexts",
        "_profile",
        "_baggage",
    )

    def __init__(  # type: ignore[misc]
        self,
        name="",  # type: str
        parent_sampled=None,  # type: Optional[bool]
        baggage=None,  # type: Optional[Baggage]
        source=TRANSACTION_SOURCE_CUSTOM,  # type: str
        **kwargs,  # type: Unpack[SpanKwargs]
    ):
        # type: (...) -> None

        super().__init__(**kwargs)

        self.name = name
        self.source = source
        self.sample_rate = None  # type: Optional[float]
        self.parent_sampled = parent_sampled
        self._measurements = {}  # type: Dict[str, MeasurementValue]
        self._contexts = {}  # type: Dict[str, Any]
        self._profile = (
            None
        )  # type: Optional[sentry_sdk.profiler.transaction_profiler.Profile]
        self._baggage = baggage

    def __repr__(self):
        # type: () -> str
        return (
            "<%s(name=%r, op=%r, trace_id=%r, span_id=%r, parent_span_id=%r, sampled=%r, source=%r, origin=%r)>"
            % (
                self.__class__.__name__,
                self.name,
                self.op,
                self.trace_id,
                self.span_id,
                self.parent_span_id,
                self.sampled,
                self.source,
                self.origin,
            )
        )

    def _possibly_started(self):
        # type: () -> bool
        """Returns whether the transaction might have been started.

        If this returns False, we know that the transaction was not started
        with sentry_sdk.start_transaction, and therefore the transaction will
        be discarded.
        """

        # We must explicitly check self.sampled is False since self.sampled can be None
        return self._span_recorder is not None or self.sampled is False

    def __enter__(self):
        # type: () -> Transaction
        if not self._possibly_started():
            logger.debug(
                "Transaction was entered without being started with sentry_sdk.start_transaction."
                "The transaction will not be sent to Sentry. To fix, start the transaction by"
                "passing it to sentry_sdk.start_transaction."
            )

        super().__enter__()

        if self._profile is not None:
            self._profile.__enter__()

        return self

    def __exit__(self, ty, value, tb):
        # type: (Optional[Any], Optional[Any], Optional[Any]) -> None
        if self._profile is not None:
            self._profile.__exit__(ty, value, tb)

        super().__exit__(ty, value, tb)

    @property
    def containing_transaction(self):
        # type: () -> Transaction
        """The root element of the span tree.
        In the case of a transaction it is the transaction itself.
        """

        # Transactions (as spans) belong to themselves (as transactions). This
        # is a getter rather than a regular attribute to avoid having a circular
        # reference.
        return self

    def finish(
        self,
        scope=None,  # type: Optional[sentry_sdk.Scope]
        end_timestamp=None,  # type: Optional[Union[float, datetime]]
    ):
        # type: (...) -> Optional[str]
        """Finishes the transaction and sends it to Sentry.
        All finished spans in the transaction will also be sent to Sentry.

        :param scope: The Scope to use for this transaction.
            If not provided, the current Scope will be used.
        :param end_timestamp: Optional timestamp that should
            be used as timestamp instead of the current time.

        :return: The event ID if the transaction was sent to Sentry,
            otherwise None.
        """
        if self.timestamp is not None:
            # This transaction is already finished, ignore.
            return None

        scope = scope or self.scope or sentry_sdk.get_current_scope()
        client = sentry_sdk.get_client()

        if not client.is_active():
            # We have no active client and therefore nowhere to send this transaction.
            return None

        if self._span_recorder is None:
            # Explicit check against False needed because self.sampled might be None
            if self.sampled is False:
                logger.debug("Discarding transaction because sampled = False")
            else:
                logger.debug(
                    "Discarding transaction because it was not started with sentry_sdk.start_transaction"
                )

            # This is not entirely accurate because discards here are not
            # exclusively based on sample rate but also traces sampler, but
            # we handle this the same here.
            if client.transport and has_tracing_enabled(client.options):
                if client.monitor and client.monitor.downsample_factor > 0:
                    reason = "backpressure"
                else:
                    reason = "sample_rate"

                client.transport.record_lost_event(reason, data_category="transaction")

                # Only one span (the transaction itself) is discarded, since we did not record any spans here.
                client.transport.record_lost_event(reason, data_category="span")
            return None

        if not self.name:
            logger.warning(
                "Transaction has no name, falling back to `<unlabeled transaction>`."
            )
            self.name = "<unlabeled transaction>"

        super().finish(scope, end_timestamp)

        if not self.sampled:
            # At this point a `sampled = None` should have already been resolved
            # to a concrete decision.
            if self.sampled is None:
                logger.warning("Discarding transaction without sampling decision.")

            return None

        finished_spans = [
            span.to_json()
            for span in self._span_recorder.spans
            if span.timestamp is not None
        ]

        # we do this to break the circular reference of transaction -> span
        # recorder -> span -> containing transaction (which is where we started)
        # before either the spans or the transaction goes out of scope and has
        # to be garbage collected
        self._span_recorder = None

        contexts = {}
        contexts.update(self._contexts)
        contexts.update({"trace": self.get_trace_context()})
        profile_context = self.get_profile_context()
        if profile_context is not None:
            contexts.update({"profile": profile_context})

        event = {
            "type": "transaction",
            "transaction": self.name,
            "transaction_info": {"source": self.source},
            "contexts": contexts,
            "tags": self._tags,
            "timestamp": self.timestamp,
            "start_timestamp": self.start_timestamp,
            "spans": finished_spans,
        }  # type: Event

        if self._profile is not None and self._profile.valid():
            event["profile"] = self._profile
            self._profile = None

        event["measurements"] = self._measurements

        return scope.capture_event(event)

    def set_measurement(self, name, value, unit=""):
        # type: (str, float, MeasurementUnit) -> None
        self._measurements[name] = {"value": value, "unit": unit}

    def set_context(self, key, value):
        # type: (str, Any) -> None
        """Sets a context. Transactions can have multiple contexts
        and they should follow the format described in the "Contexts Interface"
        documentation.

        :param key: The name of the context.
        :param value: The information about the context.
        """
        self._contexts[key] = value

    def set_http_status(self, http_status):
        # type: (int) -> None
        """Sets the status of the Transaction according to the given HTTP status.

        :param http_status: The HTTP status code."""
        super().set_http_status(http_status)
        self.set_context("response", {"status_code": http_status})

    def to_json(self):
        # type: () -> Dict[str, Any]
        """Returns a JSON-compatible representation of the transaction."""
        rv = super().to_json()

        rv["name"] = self.name
        rv["source"] = self.source
        rv["sampled"] = self.sampled

        return rv

    def get_trace_context(self):
        # type: () -> Any
        trace_context = super().get_trace_context()

        if self._data:
            trace_context["data"] = self._data

        return trace_context

    def get_baggage(self):
        # type: () -> Baggage
        """Returns the :py:class:`~sentry_sdk.tracing_utils.Baggage`
        associated with the Transaction.

        The first time a new baggage with Sentry items is made,
        it will be frozen."""

        if not self._baggage or self._baggage.mutable:
            self._baggage = Baggage.populate_from_transaction(self)

        return self._baggage

    def _set_initial_sampling_decision(self, sampling_context):
        # type: (SamplingContext) -> None
        """
        Sets the transaction's sampling decision, according to the following
        precedence rules:

        1. If a sampling decision is passed to `start_transaction`
        (`start_transaction(name: "my transaction", sampled: True)`), that
        decision will be used, regardless of anything else

        2. If `traces_sampler` is defined, its decision will be used. It can
        choose to keep or ignore any parent sampling decision, or use the
        sampling context data to make its own decision or to choose a sample
        rate for the transaction.

        3. If `traces_sampler` is not defined, but there's a parent sampling
        decision, the parent sampling decision will be used.

        4. If `traces_sampler` is not defined and there's no parent sampling
        decision, `traces_sample_rate` will be used.
        """
        client = sentry_sdk.get_client()

        transaction_description = "{op}transaction <{name}>".format(
            op=("<" + self.op + "> " if self.op else ""), name=self.name
        )

        # nothing to do if tracing is disabled
        if not has_tracing_enabled(client.options):
            self.sampled = False
            return

        # if the user has forced a sampling decision by passing a `sampled`
        # value when starting the transaction, go with that
        if self.sampled is not None:
            self.sample_rate = float(self.sampled)
            return

        # we would have bailed already if neither `traces_sampler` nor
        # `traces_sample_rate` were defined, so one of these should work; prefer
        # the hook if so
        sample_rate = (
            client.options["traces_sampler"](sampling_context)
            if callable(client.options.get("traces_sampler"))
            else (
                # default inheritance behavior
                sampling_context["parent_sampled"]
                if sampling_context["parent_sampled"] is not None
                else client.options["traces_sample_rate"]
            )
        )

        # Since this is coming from the user (or from a function provided by the
        # user), who knows what we might get. (The only valid values are
        # booleans or numbers between 0 and 1.)
        if not is_valid_sample_rate(sample_rate, source="Tracing"):
            logger.warning(
                "[Tracing] Discarding {transaction_description} because of invalid sample rate.".format(
                    transaction_description=transaction_description,
                )
            )
            self.sampled = False
            return

        self.sample_rate = float(sample_rate)

        if client.monitor:
            self.sample_rate /= 2**client.monitor.downsample_factor

        # if the function returned 0 (or false), or if `traces_sample_rate` is
        # 0, it's a sign the transaction should be dropped
        if not self.sample_rate:
            logger.debug(
                "[Tracing] Discarding {transaction_description} because {reason}".format(
                    transaction_description=transaction_description,
                    reason=(
                        "traces_sampler returned 0 or False"
                        if callable(client.options.get("traces_sampler"))
                        else "traces_sample_rate is set to 0"
                    ),
                )
            )
            self.sampled = False
            return

        # Now we roll the dice. random.random is inclusive of 0, but not of 1,
        # so strict < is safe here. In case sample_rate is a boolean, cast it
        # to a float (True becomes 1.0 and False becomes 0.0)
        self.sampled = random.random() < self.sample_rate

        if self.sampled:
            logger.debug(
                "[Tracing] Starting {transaction_description}".format(
                    transaction_description=transaction_description,
                )
            )
        else:
            logger.debug(
                "[Tracing] Discarding {transaction_description} because it's not included in the random sample (sampling rate = {sample_rate})".format(
                    transaction_description=transaction_description,
                    sample_rate=self.sample_rate,
                )
            )


class NoOpSpan(Span):
    def __repr__(self):
        # type: () -> str
        return "<%s>" % self.__class__.__name__

    @property
    def containing_transaction(self):
        # type: () -> Optional[Transaction]
        return None

    def start_child(self, **kwargs):
        # type: (**Any) -> NoOpSpan
        return NoOpSpan()

    def to_traceparent(self):
        # type: () -> str
        return ""

    def to_baggage(self):
        # type: () -> Optional[Baggage]
        return None

    def get_baggage(self):
        # type: () -> Optional[Baggage]
        return None

    def iter_headers(self):
        # type: () -> Iterator[Tuple[str, str]]
        return iter(())

    def set_tag(self, key, value):
        # type: (str, Any) -> None
        pass

    def set_data(self, key, value):
        # type: (str, Any) -> None
        pass

    def set_status(self, value):
        # type: (str) -> None
        pass

    def set_http_status(self, http_status):
        # type: (int) -> None
        pass

    def is_success(self):
        # type: () -> bool
        return True

    def to_json(self):
        # type: () -> Dict[str, Any]
        return {}

    def get_trace_context(self):
        # type: () -> Any
        return {}

    def get_profile_context(self):
        # type: () -> Any
        return {}

    def finish(
        self,
        scope=None,  # type: Optional[sentry_sdk.Scope]
        end_timestamp=None,  # type: Optional[Union[float, datetime]]
    ):
        # type: (...) -> Optional[str]
        pass

    def set_measurement(self, name, value, unit=""):
        # type: (str, float, MeasurementUnit) -> None
        pass

    def set_context(self, key, value):
        # type: (str, Any) -> None
        pass

    def init_span_recorder(self, maxlen):
        # type: (int) -> None
        pass

    def _set_initial_sampling_decision(self, sampling_context):
        # type: (SamplingContext) -> None
        pass


class POTelSpan:
    """
    OTel span wrapper providing compatibility with the old span interface.
    """

    def __init__(
        self,
        *,
        op=None,  # type: Optional[str]
        description=None,  # type: Optional[str]
        status=None,  # type: Optional[str]
        sampled=None,  # type: Optional[bool]
        start_timestamp=None,  # type: Optional[Union[datetime, float]]
        origin=None,  # type: Optional[str]
        name=None,  # type: Optional[str]
        source=TRANSACTION_SOURCE_CUSTOM,  # type: str
        attributes=None,  # type: OTelSpanAttributes
        only_if_parent=False,  # type: bool
        otel_span=None,  # type: Optional[OtelSpan]
        **_,  # type: dict[str, object]
    ):
        # type: (...) -> None
        """
        For backwards compatibility with old the old Span interface, this class
        accepts arbitrary keyword arguments, in addition to the ones explicitly
        listed in the signature. These additional arguments are ignored.

        If otel_span is passed explicitly, just acts as a proxy.

        If only_if_parent is True, just return an INVALID_SPAN
        and avoid instrumentation if there's no active parent span.
        """
        if otel_span is not None:
            self._otel_span = otel_span
        else:
            skip_span = False
            if only_if_parent:
                parent_span_context = get_current_span().get_span_context()
                skip_span = (
                    not parent_span_context.is_valid or parent_span_context.is_remote
                )

            if skip_span:
                self._otel_span = INVALID_SPAN
            else:
                from sentry_sdk.integrations.opentelemetry.consts import (
                    SentrySpanAttribute,
                )
                from sentry_sdk.integrations.opentelemetry.utils import (
                    convert_to_otel_timestamp,
                )

                if start_timestamp is not None:
                    # OTel timestamps have nanosecond precision
                    start_timestamp = convert_to_otel_timestamp(start_timestamp)

                span_name = name or description or op or ""

                # Prepopulate some attrs so that they're accessible in traces_sampler
                attributes = attributes or {}
                attributes[SentrySpanAttribute.OP] = op
                attributes[SentrySpanAttribute.SOURCE] = source
                if sampled is not None:
                    attributes[SentrySpanAttribute.CUSTOM_SAMPLED] = sampled

                self._otel_span = tracer.start_span(
                    span_name, start_time=start_timestamp, attributes=attributes
                )

                self.origin = origin or DEFAULT_SPAN_ORIGIN
                self.description = description
                self.name = span_name

                if status is not None:
                    self.set_status(status)

    def __eq__(self, other):
        # type: (POTelSpan) -> bool
        return self._otel_span == other._otel_span

    def __repr__(self):
        # type: () -> str
        return (
            "<%s(op=%r, description:%r, trace_id=%r, span_id=%r, parent_span_id=%r, sampled=%r, origin=%r)>"
            % (
                self.__class__.__name__,
                self.op,
                self.description,
                self.trace_id,
                self.span_id,
                self.parent_span_id,
                self.sampled,
                self.origin,
            )
        )

    def __enter__(self):
        # type: () -> POTelSpan
        # XXX use_span? https://github.com/open-telemetry/opentelemetry-python/blob/3836da8543ce9751051e38a110c0468724042e62/opentelemetry-api/src/opentelemetry/trace/__init__.py#L547
        #
        # create a Context object with parent set as current span
        ctx = otel_trace.set_span_in_context(self._otel_span)
        # set as the implicit current context
        self._ctx_token = context.attach(ctx)

        # get the new scope that was forked on context.attach
        self.scope = sentry_sdk.get_current_scope()
        self.scope.span = self

        return self

    def __exit__(self, ty, value, tb):
        # type: (Optional[Any], Optional[Any], Optional[Any]) -> None
        if value is not None:
            self.set_status(SPANSTATUS.INTERNAL_ERROR)

        self.finish()
        context.detach(self._ctx_token)

    @property
    def description(self):
        # type: () -> Optional[str]
        from sentry_sdk.integrations.opentelemetry.consts import SentrySpanAttribute

        return self.get_attribute(SentrySpanAttribute.DESCRIPTION)

    @description.setter
    def description(self, value):
        # type: (Optional[str]) -> None
        from sentry_sdk.integrations.opentelemetry.consts import SentrySpanAttribute

        if value is not None:
            self.set_attribute(SentrySpanAttribute.DESCRIPTION, value)

    @property
    def origin(self):
        # type: () -> Optional[str]
        from sentry_sdk.integrations.opentelemetry.consts import SentrySpanAttribute

        return self.get_attribute(SentrySpanAttribute.ORIGIN)

    @origin.setter
    def origin(self, value):
        # type: (Optional[str]) -> None
        from sentry_sdk.integrations.opentelemetry.consts import SentrySpanAttribute

        if value is not None:
            self.set_attribute(SentrySpanAttribute.ORIGIN, value)

    @property
    def containing_transaction(self):
        # type: () -> Optional[POTelSpan]
        """
        Get the transaction this span is a child of.

        .. deprecated:: 3.0.0
            This will be removed in the future. Use :func:`root_span` instead.
        """
        logger.warning(
            "Deprecated: This will be removed in the future. Use root_span instead."
        )
        return self.root_span

    @property
    def root_span(self):
        # type: () -> Optional[POTelSpan]
        from sentry_sdk.integrations.opentelemetry.utils import (
            get_sentry_meta,
        )

        root_otel_span = cast(
            "Optional[OtelSpan]", get_sentry_meta(self._otel_span, "root_span")
        )
        return POTelSpan(otel_span=root_otel_span) if root_otel_span else None

    @property
    def is_root_span(self):
        # type: () -> bool
        return self.root_span == self

    @property
    def parent_span_id(self):
        # type: () -> Optional[str]
        if (
            not isinstance(self._otel_span, ReadableSpan)
            or self._otel_span.parent is None
        ):
            return None
        return format_span_id(self._otel_span.parent.span_id)

    @property
    def trace_id(self):
        # type: () -> str
        return format_trace_id(self._otel_span.get_span_context().trace_id)

    @property
    def span_id(self):
        # type: () -> str
        return format_span_id(self._otel_span.get_span_context().span_id)

    @property
    def is_valid(self):
        # type: () -> bool
        return self._otel_span.get_span_context().is_valid

    @property
    def sampled(self):
        # type: () -> Optional[bool]
        return self._otel_span.get_span_context().trace_flags.sampled

    @property
    def op(self):
        # type: () -> Optional[str]
        from sentry_sdk.integrations.opentelemetry.consts import SentrySpanAttribute

        return self.get_attribute(SentrySpanAttribute.OP)

    @op.setter
    def op(self, value):
        # type: (Optional[str]) -> None
        from sentry_sdk.integrations.opentelemetry.consts import SentrySpanAttribute

        if value is not None:
            self.set_attribute(SentrySpanAttribute.OP, value)

    @property
    def name(self):
        # type: () -> Optional[str]
        from sentry_sdk.integrations.opentelemetry.consts import SentrySpanAttribute

        return self.get_attribute(SentrySpanAttribute.NAME)

    @name.setter
    def name(self, value):
        # type: (Optional[str]) -> None
        from sentry_sdk.integrations.opentelemetry.consts import SentrySpanAttribute

        if value is not None:
            self.set_attribute(SentrySpanAttribute.NAME, value)

    @property
    def source(self):
        # type: () -> str
        from sentry_sdk.integrations.opentelemetry.consts import SentrySpanAttribute

        return (
            self.get_attribute(SentrySpanAttribute.SOURCE) or TRANSACTION_SOURCE_CUSTOM
        )

    @source.setter
    def source(self, value):
        # type: (str) -> None
        from sentry_sdk.integrations.opentelemetry.consts import SentrySpanAttribute

        self.set_attribute(SentrySpanAttribute.SOURCE, value)

    @property
    def start_timestamp(self):
        # type: () -> Optional[datetime]
        if not isinstance(self._otel_span, ReadableSpan):
            return None

        start_time = self._otel_span.start_time
        if start_time is None:
            return None

        from sentry_sdk.integrations.opentelemetry.utils import (
            convert_from_otel_timestamp,
        )

        return convert_from_otel_timestamp(start_time)

    @property
    def timestamp(self):
        # type: () -> Optional[datetime]
        if not isinstance(self._otel_span, ReadableSpan):
            return None

        end_time = self._otel_span.end_time
        if end_time is None:
            return None

        from sentry_sdk.integrations.opentelemetry.utils import (
            convert_from_otel_timestamp,
        )

        return convert_from_otel_timestamp(end_time)

    def start_child(self, **kwargs):
        # type: (**Any) -> POTelSpan
        kwargs.setdefault("sampled", self.sampled)

        span = POTelSpan(only_if_parent=True, **kwargs)
        return span

    def iter_headers(self):
        # type: () -> Iterator[Tuple[str, str]]
        yield SENTRY_TRACE_HEADER_NAME, self.to_traceparent()

        from sentry_sdk.integrations.opentelemetry.utils import (
            serialize_trace_state,
        )

        yield BAGGAGE_HEADER_NAME, serialize_trace_state(self.trace_state)

    def to_traceparent(self):
        # type: () -> str
        if self.sampled is True:
            sampled = "1"
        elif self.sampled is False:
            sampled = "0"
        else:
            sampled = None

        traceparent = "%s-%s" % (self.trace_id, self.span_id)
        if sampled is not None:
            traceparent += "-%s" % (sampled,)

        return traceparent

    @property
    def trace_state(self):
        # type: () -> TraceState
        from sentry_sdk.integrations.opentelemetry.utils import (
            get_trace_state,
        )

        return get_trace_state(self._otel_span)

    def to_baggage(self):
        # type: () -> Baggage
        return self.get_baggage()

    def get_baggage(self):
        # type: () -> Baggage
        from sentry_sdk.integrations.opentelemetry.utils import (
            baggage_from_trace_state,
        )

        return baggage_from_trace_state(self.trace_state)

    def set_tag(self, key, value):
        # type: (str, Any) -> None
        from sentry_sdk.integrations.opentelemetry.consts import SentrySpanAttribute

        self.set_attribute(f"{SentrySpanAttribute.TAG}.{key}", value)

    def set_data(self, key, value):
        # type: (str, Any) -> None
        # TODO-neel-potel we cannot add dicts here
        self.set_attribute(key, value)

    def get_attribute(self, name):
        # type: (str) -> Optional[Any]
        if not isinstance(self._otel_span, ReadableSpan):
            return None
        return self._otel_span.attributes.get(name)

    def set_attribute(self, key, value):
        # type: (str, Any) -> None
        self._otel_span.set_attribute(key, _serialize_span_attribute(value))

    def set_status(self, status):
        # type: (str) -> None
        if status == SPANSTATUS.OK:
            otel_status = StatusCode.OK
            otel_description = None
        else:
            otel_status = StatusCode.ERROR
            otel_description = status

        self._otel_span.set_status(otel_status, otel_description)

    def set_measurement(self, name, value, unit=""):
        # type: (str, float, MeasurementUnit) -> None
        from sentry_sdk.integrations.opentelemetry.consts import SentrySpanAttribute

        # Stringify value here since OTel expects all seq items to be of one type
        self.set_attribute(
            f"{SentrySpanAttribute.MEASUREMENT}.{name}", (str(value), unit)
        )

    def set_thread(self, thread_id, thread_name):
        # type: (Optional[int], Optional[str]) -> None
        if thread_id is not None:
            self.set_data(SPANDATA.THREAD_ID, str(thread_id))

            if thread_name is not None:
                self.set_data(SPANDATA.THREAD_NAME, thread_name)

    def update_active_thread(self):
        # type: () -> None
        thread_id, thread_name = get_current_thread_meta()
        self.set_thread(thread_id, thread_name)

    def set_http_status(self, http_status):
        # type: (int) -> None
        self.set_data(SPANDATA.HTTP_STATUS_CODE, http_status)
        self.set_status(get_span_status_from_http_code(http_status))

    def is_success(self):
        # type: () -> bool
        return self._otel_span.status.code == StatusCode.OK

    def finish(self, end_timestamp=None):
        # type: (Optional[Union[float, datetime]]) -> Optional[str]
        if end_timestamp is not None:
            from sentry_sdk.integrations.opentelemetry.utils import (
                convert_to_otel_timestamp,
            )

            self._otel_span.end(convert_to_otel_timestamp(end_timestamp))
        else:
            self._otel_span.end()

    def to_json(self):
        # type: () -> dict[str, Any]
        # TODO-neel-potel for sampling context
        pass

    def get_trace_context(self):
        # type: () -> dict[str, Any]
        if not isinstance(self._otel_span, ReadableSpan):
            return {}

        from sentry_sdk.integrations.opentelemetry.utils import (
            get_trace_context,
        )

        return get_trace_context(self._otel_span)

    def set_context(self, key, value):
        # type: (str, Any) -> None
        from sentry_sdk.integrations.opentelemetry.consts import SentrySpanAttribute

        # TODO-neel-potel we cannot add dicts here

        self.set_attribute(f"{SentrySpanAttribute.CONTEXT}.{key}", value)


if TYPE_CHECKING:

    @overload
    def trace(func=None):
        # type: (None) -> Callable[[Callable[P, R]], Callable[P, R]]
        pass

    @overload
    def trace(func):
        # type: (Callable[P, R]) -> Callable[P, R]
        pass


def trace(func=None):
    # type: (Optional[Callable[P, R]]) -> Union[Callable[P, R], Callable[[Callable[P, R]], Callable[P, R]]]
    """
    Decorator to start a child span under the existing current transaction.
    If there is no current transaction, then nothing will be traced.

    .. code-block::
        :caption: Usage

        import sentry_sdk

        @sentry_sdk.trace
        def my_function():
            ...

        @sentry_sdk.trace
        async def my_async_function():
            ...
    """
    from sentry_sdk.tracing_utils import start_child_span_decorator

    # This patterns allows usage of both @sentry_traced and @sentry_traced(...)
    # See https://stackoverflow.com/questions/52126071/decorator-with-arguments-avoid-parenthesis-when-no-arguments/52126278
    if func:
        return start_child_span_decorator(func)
    else:
        return start_child_span_decorator


# Circular imports

from sentry_sdk.tracing_utils import (
    Baggage,
    EnvironHeaders,
    extract_sentrytrace_data,
    has_tracing_enabled,
    maybe_create_breadcrumbs_from_span,
)
